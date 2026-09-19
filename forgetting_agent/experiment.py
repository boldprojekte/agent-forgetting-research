"""Task fixtures, grading, arms, run directories and honest size/usage accounting.

A run writes to `<runs_dir>/<session_id>/` (the directory must not exist yet; there is no
resume):
- trace.jsonl: every request/response body, tool call/result, context transition, recovery,
  termination and grade (no credentials; see provider.py).
- summary.json: termination, answer, correctness, request sizes, provider usage as reported.
- archive.json: the archived originals and stubs, for inspection only (never sent to the model).
- final_history.json: the active history as it stood when the episode ended.

Sizes are JSON characters/bytes of the exact request bodies. They are not token counts.
Provider usage fields (incl. reasoning and cache fields) are copied verbatim when present.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .costs import estimate
from .loop import LoopConfig, Model, prompt_fingerprint, rendered_tools_for, run_episode
from .scripted import result_id_for_path
from .tools import Corpus
from .trace import Trace

# The model's tools see only `corpus/<name>/`. Task specifications (question, gold answer,
# expected fields) live in `tasks/<name>.json`, outside every corpus root, so no tool can list,
# read or search the answer key.
CORPUS_DIR = Path(__file__).parent / "corpus"
TASKS_DIR = Path(__file__).parent / "tasks"


@dataclass(frozen=True)
class Task:
    """One question over one corpus with an exact, field-wise grading rule.

    `answer_pattern` is a regex with named groups; it must match the whole answer (surrounding
    whitespace ignored, case-insensitive). `expected_fields` maps each group to either
    `{"number": x}` (numeric equality) or `{"text": s}` (case- and whitespace-insensitive
    equality). An empty mapping means the pattern alone decides.
    """

    task_id: str
    question: str
    corpus_root: Path
    gold_answer: str
    answer_pattern: str
    expected_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: str = ""
    spec_path: Path | None = None


@dataclass(frozen=True)
class Grade:
    correct: bool
    missing: list[str]  # field names that were absent or wrong; all fields when unparsable
    fields: dict[str, str] = field(default_factory=dict)  # parsed values, for the trace


def load_task(name: str) -> Task:
    spec_path = TASKS_DIR / f"{name}.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return Task(
        task_id=spec["task_id"],
        question=spec["question"],
        corpus_root=CORPUS_DIR / spec["corpus"],
        gold_answer=spec["gold_answer"],
        answer_pattern=spec["answer_pattern"],
        expected_fields=dict(spec.get("expected_fields", {})),
        notes=spec.get("notes", ""),
        spec_path=spec_path,
    )


def grade(task: Task, answer: str | None) -> Grade:
    """Parse the requested fields from the answer and compare each one exactly.

    No substring matching: "FFL=142.90" is not 42.90 and "Not Nordholz" is not Nordholz.
    """
    names = list(task.expected_fields)
    if not answer:
        return Grade(False, names)
    match = re.fullmatch(rf"\s*(?:{task.answer_pattern})\s*", answer, flags=re.IGNORECASE)
    if match is None:
        return Grade(False, names)
    parsed = {name: match.group(name) for name in names}
    missing = [
        name for name in names if not _field_matches(task.expected_fields[name], parsed[name])
    ]
    return Grade(not missing, missing, parsed)


def _field_matches(expected: dict[str, Any], value: str) -> bool:
    if "number" in expected:
        try:
            return abs(float(value) - float(expected["number"])) < 1e-9
        except ValueError:
            return False
    return _normalise_text(value) == _normalise_text(str(expected["text"]))


def _normalise_text(text: str) -> str:
    return " ".join(text.casefold().split())


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def run_task(
    task: Task,
    model: Model,
    config: LoopConfig,
    runs_dir: Path,
    session_id: str | None = None,
    provider_info: dict[str, Any] | None = None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    session = session_id or new_session_id()
    run_dir = Path(runs_dir) / session
    if run_dir.exists():
        raise FileExistsError(
            f"run directory {run_dir} already exists; a session ID is used once and there is no "
            "resume. Choose a new --session-id."
        )
    run_dir.mkdir(parents=True)
    trace = Trace(run_dir / "trace.jsonl")
    corpus = Corpus(task.corpus_root)
    try:
        trace.event(
            "run_start",
            session_id=session,
            task_id=task.task_id,
            task_notes=task.notes,
            model=model.name,
            provider=provider_info or {"provider": "scripted-fixture"},
            config=asdict(config),
            prompt=prompt_fingerprint(config.arm)
            if system_prompt is None
            else {"sha256": "custom", "text": system_prompt},
            tools=rendered_tools_for(config.arm, corpus, config.required_submission_criteria),
        )
        result = run_episode(
            user_task=task.question,
            tools=corpus,
            model=model,
            config=config,
            trace=trace,
            system_prompt=system_prompt,
        )
        verdict = grade(task, result.answer)
        trace.event(
            "grade",
            correct=verdict.correct,
            missing=verdict.missing,
            fields=verdict.fields,
            answer=result.answer,
            gold_answer=task.gold_answer,
        )
    finally:
        trace.close()

    summary = summarise_run(trace, result, model, config, task.task_id, session, run_dir)
    summary["gold_answer"] = task.gold_answer
    summary["correct"] = verdict.correct
    summary["missing_facts"] = verdict.missing
    write_run_files(run_dir, summary, result.state)
    return summary


def summarise_run(
    trace: Trace,
    result: Any,
    model: Model,
    config: LoopConfig,
    task_id: str,
    session: str,
    run_dir: Path,
) -> dict[str, Any]:
    """Termination, answer, request sizes and provider usage from the trace. No grading here."""
    requests = [e for e in trace.events if e["event"] == "request"]
    usage = [e["usage"] for e in trace.events if e["event"] == "response" and e.get("usage")]
    sizes = [r["chars"] for r in requests]
    return {
        "session_id": session,
        "task_id": task_id,
        "arm": config.arm,
        "model": model.name,
        "termination": result.termination,
        "error": result.error,
        "steps": result.steps,
        "answer": result.answer,
        "requests": len(requests),  # logical turns, retained for existing trace consumers
        "provider_attempts": sum(e["event"] == "provider_attempt" for e in trace.events),
        "max_provider_attempts": (
            config.max_provider_attempts
            if config.max_provider_attempts is not None
            else config.max_calls
        ),
        "request_chars": {
            "min": min(sizes) if sizes else 0,
            "max": max(sizes) if sizes else 0,
            "total": sum(sizes),
        },
        "request_bytes_total": sum(r["bytes"] for r in requests),
        "context_transitions": sum(1 for e in trace.events if e["event"] == "context_transition"),
        "recoveries": sum(1 for e in trace.events if e["event"] == "recovery" and e["ok"]),
        "batch_rejections": sum(1 for e in trace.events if e["event"] == "batch_rejected"),
        "usage": usage,
        "costs": estimate(trace.events),
        "run_dir": str(run_dir),
    }


def write_run_files(run_dir: Path, summary: dict[str, Any], state: Any) -> None:
    """summary.json, archive.json (inspection only) and final_history.json."""
    _write_json(run_dir / "summary.json", summary)
    _write_json(
        run_dir / "archive.json",
        {
            "entries": [asdict(entry) for entry in state.archive.entries.values()],
            "stubs": state.stubs,
            "result_ids": state.result_ids,
        },
    )
    _write_json(run_dir / "final_history.json", state.messages)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# -- prescribed demo scripts for the scripted fixture ---------------------------------------------
# These scripts are PRESCRIBED sequences for the offline demonstration. They show the harness
# mechanics; they say nothing about what a model would choose.


def method_demo_script() -> list:
    def apply_step(messages: list[dict[str, Any]]) -> list:
        result_id = result_id_for_path(messages, "regulations-excerpt.md")
        note = (
            "Regulatory excerpt read; clause 4.3.2 sets the public-room FFL relative to the "
            "design flood level."
        )
        return [("context_apply", {"targets": [{"id": result_id, "note": note}]})]

    def recover_step(messages: list[dict[str, Any]]) -> list:
        stub = next(
            m["content"] for m in messages if (m.get("content") or "").startswith("[set aside")
        )
        return [("context_recover", {"ref": stub.rsplit("ref:", 1)[1].rstrip("]")})]

    return [
        [("list_files", {})],
        [("read_file", {"path": "regulations-excerpt.md"})],
        [("read_file", {"path": "site-survey.md"})],
        apply_step,
        [("read_file", {"path": "meeting-notes-2026-03.md"})],
        [("read_file", {"path": "contractor-emails.md"})],
        recover_step,
        [("submit_answer", {"answer": "FFL=42.90 m AOD; roof=Nordholz Timber GmbH"})],
    ]


def retained_demo_script() -> list:
    return [
        [("list_files", {})],
        [("read_file", {"path": "regulations-excerpt.md"})],
        [("read_file", {"path": "site-survey.md"})],
        [("read_file", {"path": "meeting-notes-2026-03.md"})],
        [("read_file", {"path": "contractor-emails.md"})],
        [("submit_answer", {"answer": "FFL=42.90 m AOD; roof=Nordholz Timber GmbH"})],
    ]
