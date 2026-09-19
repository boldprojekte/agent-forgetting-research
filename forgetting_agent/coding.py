"""Coding task runner: per-run snapshot, sandboxed tools, trusted parent-side grader, demo scripts.

A coding task lives in `coding_tasks/<name>/`:
- task.json: instructions, notes, snapshot directory, grader file, timeout, optional `tests`
  block (`TestSpec`: what run_tests may select; default pytest node ids) and, for a derived
  fixture, a `source` block (see omarchy_snapshot.py) with `manifest.json` next to it;
- snapshot/: the code the model works on (copied fresh into every run, never modified here);
- grader.json: expected values that never leave the host. The model-visible tests inside the
  snapshot can be edited by the model and are therefore not grading evidence. Kinds:
  `python` (default): `module` plus cases `{function, args, expected}`;
  `shell`: `program` (fixed argv prefix, e.g. `["bash", "bin/tool"]`) plus cases
  `{args, expected: {exit, stderr_contains?, stdout_contains?, no_output?}}`;
  `theme`: the historical theme pilot's fixed observation worker builds disposable fixtures,
  invokes theme-set and its real template pipeline, and reports values for parent-side
  comparison. `fixture` is input only; expectations never go into the sandbox worker.
  The task-specific grade status separates assertion failures from inconclusive execution.

Grading (`grade`): the harness runs a small worker (stdlib only, no expectations) inside the
sandbox on a fresh tmpfs copy of the final workspace, with export disabled. The worker
receives only the per-case inputs on stdin (module/function/args, or program/args), runs
each case and prints one JSON record per case. The harness requires a completed run (trusted
bootstrap report, exit 0, no timeout or kill, untruncated output), exactly one well-formed
record per case, a matching completion record, and a match against the expectation it kept
(value plus type equality for python; exit status and message fragments for shell). Anything
the candidate prints or any exit status it chooses cannot produce a pass without the correct
behaviour: spoofed records are either wrong, conflicting, duplicated or missing, and each of
those is a recorded failure with its reason. This is tamper-resistant grading for fixed
fixtures; it is not a claim about grading arbitrary adversarial projects.

Run layout under `<runs_dir>/<session_id>/`: trace.jsonl, summary.json, archive.json,
final_history.json (as for corpus runs), plus `snapshot/` (pristine copy), `workspace/` (the
model's tree at episode end), `diff.patch` and `changes.json` (typed manifest).

Coding mode needs a verified sandbox (`sandbox.preflight()` ok). Without one `run_coding_task`
raises `SandboxUnavailable` before creating anything; no tool ever executes task code on the
host.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .experiment import new_session_id, summarise_run, write_run_files
from .loop import (
    LoopConfig,
    Model,
    build_system_prompt,
    prompt_fingerprint,
    rendered_tools_for,
    run_episode,
)
from .sandbox import Sandbox, SandboxUnavailable
from .scripted import result_id_for_path
from .trace import Trace
from .workspace import TestSpec, Workspace

CODING_TASKS_DIR = Path(__file__).parent / "coding_tasks"
CODING_PROMPT = "coding_shared.md"
GRADER_OUTPUT_CHARS = 8_000
GRADER_MAX_FAILURES = 20
GRADER_MAX_OUTPUT_BYTES = 256_000

# Runs inside the sandbox with `python -c`; sees only inputs. Records are one JSON object per
# line; the last one carries `grader_done`. Every exception becomes an `error` record.
GRADER_WORKER = """
import importlib, json, sys
job = json.load(sys.stdin)
for case in job["cases"]:
    try:
        function = getattr(importlib.import_module(job["module"]), case["function"])
        record = {"id": case["id"], "value": function(*case["args"])}
    except BaseException as error:
        record = {"id": case["id"], "error": f"{type(error).__name__}: {error}"[:300]}
    try:
        line = json.dumps(record)
    except (TypeError, ValueError):
        line = json.dumps({"id": case["id"], "error": "not JSON: " + repr(record["value"])[:200]})
    print(line, flush=True)
print(json.dumps({"grader_done": len(job["cases"])}), flush=True)
"""

# Shell kind: runs `program + args` per case (no shell, stdin closed, per-case timeout) and
# records exit status and bounded output. Expectations stay on the host.
SHELL_GRADER_WORKER = """
import json, subprocess, sys
job = json.load(sys.stdin)
for case in job["cases"]:
    try:
        run = subprocess.run(
            [*job["program"], *case["args"]], capture_output=True,
            stdin=subprocess.DEVNULL, timeout=job["case_timeout_s"],
        )
        record = {"id": case["id"], "value": {
            "exit": run.returncode,
            "stdout": run.stdout.decode("utf-8", "replace")[:2000],
            "stderr": run.stderr.decode("utf-8", "replace")[:2000],
        }}
    except BaseException as error:
        record = {"id": case["id"], "error": f"{type(error).__name__}: {error}"[:300]}
    print(json.dumps(record), flush=True)
print(json.dumps({"grader_done": len(job["cases"])}), flush=True)
"""
SHELL_CASE_TIMEOUT_S = 10.0
GRADER_KINDS = (
    "python",
    "shell",
    "theme",
    "sqlite_csv",
    "ledger_workflow",
    "ledger_project_plan",
    "sphinx_requirements",
    "sphinx_manifest",
)


@dataclass(frozen=True)
class CodingTask:
    task_id: str
    instructions: str
    snapshot_dir: Path
    grader_path: Path
    test_timeout_s: float
    notes: str = ""
    spec_path: Path | None = None
    tests: TestSpec = TestSpec()


def load_coding_task(name: str) -> CodingTask:
    spec_path = CODING_TASKS_DIR / name / "task.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    base = spec_path.parent
    return CodingTask(
        task_id=spec["task_id"],
        instructions=spec["instructions"],
        snapshot_dir=base / spec["snapshot"],
        grader_path=base / spec["grader"],
        test_timeout_s=float(spec.get("test_timeout_s", 60)),
        notes=spec.get("notes", ""),
        spec_path=spec_path,
        tests=TestSpec.from_spec(spec.get("tests")),
    )


def run_coding_task(
    task: CodingTask,
    model: Model,
    config: LoopConfig,
    runs_dir: Path,
    session_id: str | None = None,
    provider_info: dict[str, Any] | None = None,
    sandbox: Sandbox | None = None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    if sandbox is None:
        raise SandboxUnavailable(
            "coding mode is blocked: no verified sandbox (run sandbox.preflight() and fix the "
            "failing check). Task code is never executed on the host."
        )
    session = session_id or new_session_id()
    run_dir = Path(runs_dir) / session
    if run_dir.exists():
        raise FileExistsError(
            f"run directory {run_dir} already exists; a session ID is used once and there is no "
            "resume. Choose a new --session-id."
        )
    run_dir.mkdir(parents=True)
    workspace = Workspace.from_snapshot(task.snapshot_dir, run_dir, sandbox, tests=task.tests)
    prompt = system_prompt or build_system_prompt(config.arm, CODING_PROMPT)
    trace = Trace(run_dir / "trace.jsonl")
    try:
        trace.event(
            "run_start",
            session_id=session,
            task_id=task.task_id,
            task_kind="coding",
            task_notes=task.notes,
            model=model.name,
            provider=provider_info or {"provider": "scripted-fixture"},
            config=asdict(config),
            prompt=prompt_fingerprint(config.arm, CODING_PROMPT)
            if system_prompt is None
            else {"sha256": "custom", "file": "custom", "text": system_prompt},
            tools=rendered_tools_for(config.arm, workspace, config.required_submission_criteria),
            sandbox=sandbox.report.describe(),
            test_timeout_s=task.test_timeout_s,
        )
        result = run_episode(
            user_task=task.instructions,
            tools=workspace,
            model=model,
            config=config,
            trace=trace,
            system_prompt=prompt,
        )
        grading = grade(task, workspace.root, sandbox, timeout_s=task.test_timeout_s)
        trace.event("grade", correct=grading["correct"], answer=result.answer, grading=grading)
    finally:
        trace.close()

    summary = summarise_run(trace, result, model, config, task.task_id, session, run_dir)
    summary["task_kind"] = "coding"
    summary["correct"] = grading["correct"]
    summary["grading"] = grading
    summary["sandbox"] = sandbox.report.describe()
    changes = workspace.changes()
    (run_dir / "diff.patch").write_text(workspace.diff().text + "\n", encoding="utf-8")
    (run_dir / "changes.json").write_text(
        json.dumps([asdict(c) for c in changes], indent=2) + "\n", encoding="utf-8"
    )
    write_run_files(run_dir, summary, result.state)
    return summary


def grade(task: CodingTask, workspace_root: Path, sandbox: Sandbox, timeout_s: float) -> dict:
    """Trusted grading of the final workspace; see the module docstring for the protocol."""
    spec = json.loads(task.grader_path.read_text(encoding="utf-8"))
    cases = spec["cases"]
    kind = spec.get("kind", "python")
    if kind not in GRADER_KINDS:
        raise ValueError(f"unknown grader kind {kind!r} in {task.grader_path}")
    if kind in (
        "theme",
        "sqlite_csv",
        "ledger_workflow",
        "ledger_project_plan",
        "sphinx_requirements",
        "sphinx_manifest",
    ):
        filename = "historical_theme_worker.py" if kind == "theme" else f"{kind}_worker.py"
        worker = Path(__file__).with_name(filename).read_text(encoding="utf-8")
        job = {"cases": [{"id": i, "fixture": c["fixture"]} for i, c in enumerate(cases)]}
    elif kind == "shell":
        worker = SHELL_GRADER_WORKER
        job: dict[str, Any] = {
            "program": list(spec["program"]),
            "case_timeout_s": min(SHELL_CASE_TIMEOUT_S, timeout_s),
            "cases": [{"id": i, "args": list(c["args"])} for i, c in enumerate(cases)],
        }
    else:
        worker = GRADER_WORKER
        job = {
            "module": spec["module"],
            "cases": [
                {"id": i, "function": c["function"], "args": c["args"]} for i, c in enumerate(cases)
            ],
        }
    outcome = sandbox.run_python(
        workspace_root,
        ["-c", worker],
        timeout_s=timeout_s,
        max_output_bytes=GRADER_MAX_OUTPUT_BYTES,
        stdin_text=json.dumps(job),
        export=False,
    )
    run = outcome.describe()
    output = outcome.stdout + (
        "\n--- stderr ---\n" + outcome.stderr if outcome.stderr.strip() else ""
    )
    run["stdout"] = outcome.stdout[:GRADER_OUTPUT_CHARS]
    run["stderr"] = outcome.stderr[:GRADER_OUTPUT_CHARS]
    result: dict[str, Any] = {
        "correct": False,
        "reason": None,
        "cases": len(cases),
        "passed": 0,
        "failures": [],
        "run": run,
        "output": output[:GRADER_OUTPUT_CHARS],
    }
    problems: list[str] = []
    if outcome.timed_out:
        problems.append(f"grader run timed out after {timeout_s}s")
    elif outcome.error:
        problems.append(outcome.error)
    elif outcome.killed:
        problems.append(outcome.killed)
    elif outcome.exit_code != 0:
        problems.append(f"grader run exited {outcome.exit_code}")
    if outcome.truncated:
        problems.append("grader output truncated")
    records, parse_problems = _parse_records(outcome.stdout, len(cases))
    problems += parse_problems
    failures: list[dict] = []
    passed = 0
    for i, case in enumerate(cases):
        record = records.get(i)
        if record is None:
            actual: Any = "<missing>"
        elif "error" in record:
            actual = f"<error {record['error']}>"
        else:
            actual = record.get("value")
            if _matches(kind, actual, case["expected"]):
                passed += 1
                continue
        failure = {"id": i, "args": case["args"], "expected": case["expected"], "actual": actual}
        if kind == "python":
            failure["function"] = case["function"]
        failures.append(failure)
    missing = [f["id"] for f in failures if f["actual"] == "<missing>"]
    if missing:
        problems.append(f"missing records for {len(missing)} case(s)")
    if failures:
        problems.append(f"{len(failures)} of {len(cases)} cases failed")
    result["passed"] = passed
    result["failures"] = failures[:GRADER_MAX_FAILURES]
    failed_ids = {failure["id"] for failure in failures}
    result["case_results"] = [
        {"id": i, "passed": i not in failed_ids, "requirements": case.get("requirements", [])}
        for i, case in enumerate(cases)
    ]
    result["correct"] = not problems and passed == len(cases)
    result["reason"] = "; ".join(problems) or None
    if kind in (
        "theme",
        "sqlite_csv",
        "ledger_workflow",
        "ledger_project_plan",
        "sphinx_requirements",
        "sphinx_manifest",
    ):
        unavailable = (
            not outcome.completed
            or outcome.exit_code != 0
            or outcome.truncated
            or bool(parse_problems)
            or any("error" in r for r in records.values())
        )
        result["status"] = (
            "inconclusive"
            if unavailable
            else "passed"
            if result["correct"]
            else "assertion_failure"
        )
    if kind in ("ledger_workflow", "ledger_project_plan"):
        lint = sandbox.run_python(
            workspace_root,
            [
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--no-cache",
                "--target-version",
                "py311",
                "--line-length",
                "100",
                "--select",
                "E,F,I,W,B,UP",
                ".",
            ],
            timeout_s=timeout_s,
            export=False,
        )
        protected = [
            task.snapshot_dir / "README.md",
            *(p for p in (task.snapshot_dir / "docs").rglob("*") if p.is_file()),
        ]
        changed = []
        for original in protected:
            relative = original.relative_to(task.snapshot_dir)
            candidate = workspace_root / relative
            if (
                candidate.resolve() != candidate.absolute()
                or not candidate.is_file()
                or candidate.stat().st_size != original.stat().st_size
                or candidate.read_bytes() != original.read_bytes()
            ):
                changed.append(str(relative))
        result["quality"] = {
            "ruff": lint.describe(),
            "functional_correct": result["correct"],
            "preserved_contract": not changed,
            "changed_contract_files": changed,
        }
    return result


def _parse_records(stdout: str, count: int) -> tuple[dict[int, dict], list[str]]:
    """Exactly one JSON record per case id in 0..count-1 and one completion record."""
    records: dict[int, dict] = {}
    problems: list[str] = []
    done_seen = False
    malformed = 0
    conflicting: set[int] = set()
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        if "grader_done" in record:
            if done_seen or record.get("grader_done") != count:
                problems.append("completion record duplicated or wrong")
            done_seen = True
            continue
        case_id = record.get("id")
        if not isinstance(case_id, int) or isinstance(case_id, bool) or not 0 <= case_id < count:
            malformed += 1
            continue
        if ("value" in record) == ("error" in record):
            malformed += 1
            continue
        if case_id in records and records[case_id] != record:
            conflicting.add(case_id)
        records.setdefault(case_id, record)
    if malformed:
        problems.append(f"{malformed} malformed record(s)")
    if conflicting:
        problems.append(f"conflicting records for case(s) {sorted(conflicting)}")
        for case_id in conflicting:
            records.pop(case_id, None)
    if not done_seen:
        problems.append("no completion record")
    return records, problems


def _same(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected


def _matches(kind: str, actual: Any, expected: Any) -> bool:
    if kind in (
        "sqlite_csv",
        "ledger_workflow",
        "ledger_project_plan",
        "sphinx_requirements",
        "sphinx_manifest",
    ):
        if type(actual) is not type(expected):
            return False
        if isinstance(expected, dict):
            return actual.keys() == expected.keys() and all(
                _matches(kind, actual[key], value) for key, value in expected.items()
            )
        if isinstance(expected, list):
            return len(actual) == len(expected) and all(
                _matches(kind, a, e) for a, e in zip(actual, expected, strict=True)
            )
        return actual == expected
    if kind in ("python", "theme"):
        return _same(actual, expected)
    return _shell_case_ok(actual, expected)


def _shell_case_ok(actual: Any, expected: dict) -> bool:
    """Exit status must match; message fragments are matched case-insensitively."""
    if not isinstance(actual, dict) or not isinstance(actual.get("exit"), int):
        return False
    stdout, stderr = actual.get("stdout"), actual.get("stderr")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return False
    if actual["exit"] != expected["exit"]:
        return False
    if expected.get("no_output") and (stdout.strip() or stderr.strip()):
        return False
    for key, text in (("stderr_contains", stderr), ("stdout_contains", stdout)):
        fragment = expected.get(key)
        if fragment is not None and fragment.lower() not in text.lower():
            return False
    return True


# -- prescribed demo scripts for the scripted fixture ---------------------------------------------
# PRESCRIBED sequences for the offline walkthrough of the textstats fixture. They exercise the
# harness (tools, sandbox, context operations, grader); they say nothing about what a model
# would choose.

FIX_OLD = 'return len(text.split(" "))'
FIX_NEW = "return len(text.split())"
FIX_SUMMARY = (
    "word_count now splits on any whitespace with str.split(), so runs of spaces, tabs and "
    "newlines separate words and surrounding whitespace adds nothing; line_count and "
    "longest_word are unchanged."
)


def _apply_readme(messages: list[dict[str, Any]]) -> list:
    result_id = result_id_for_path(messages, "README.md")
    note = (
        "README read; conventions: any whitespace separates words, surrounding whitespace "
        "adds no words, functions never raise on empty input."
    )
    return [("context_apply", {"targets": [{"id": result_id, "note": note}]})]


def coding_demo_script() -> list:
    return [
        [("list_files", {})],
        [("read_file", {"path": "README.md"})],
        [("read_file", {"path": "textstats/__init__.py"})],
        [("run_tests", {"tests": ["tests"]})],
        [("grep", {"pattern": 'split(" ")', "path_glob": "textstats/**"})],
        [
            (
                "edit_file",
                {"path": "textstats/__init__.py", "old_text": FIX_OLD, "new_text": FIX_NEW},
            )
        ],
        [("run_tests", {"tests": ["tests/test_textstats.py"]})],
        _apply_readme,
        [("diff", {})],
        [("submit_answer", {"answer": FIX_SUMMARY})],
    ]


def _apply_matching(fragment: str, note: str):
    """Step: set aside the read result for the path `fragment`."""

    def step(messages: list[dict[str, Any]]) -> list:
        result_id = result_id_for_path(messages, fragment)
        return [("context_apply", {"targets": [{"id": result_id, "note": note}]})]

    return step


OMARCHY_SCRIPT = "bin/omarchy-git-url-check"
OMARCHY_TEST = "test/shell.d/git-url-check-test.sh"
OMARCHY_SUMMARY = (
    "omarchy-git-url-check now refuses a URL containing whitespace with exit 1 and a message "
    "naming it; the check runs after the option/helper and transport refusals so those keep "
    "their messages, and the transport loop sets a flag instead of exiting early. The test "
    "file gained assertions for whitespace refusal and for the refusal order."
)


def omarchy_demo_script(task: CodingTask, retained: bool = False) -> list:
    """PRESCRIBED walkthrough of the Omarchy git-url-check task using the reference edits."""
    edits = json.loads(
        (task.spec_path.parent / "reference_edits.json").read_text(encoding="utf-8")
    )["edits"]
    by_path = {e["path"]: e for e in edits}
    steps: list = [
        [("list_files", {})],
        [("read_file", {"path": OMARCHY_SCRIPT})],
        [("read_file", {"path": "AGENTS.md"})],
        [("grep", {"pattern": "omarchy-git-url-check", "path_glob": "bin/**"})],
        [("read_file", {"path": OMARCHY_TEST})],
        [("run_tests", {"tests": [OMARCHY_TEST]})],
    ]
    if not retained:
        steps += [
            _apply_matching(
                "AGENTS.md",
                "AGENTS.md read; style: [[ ]] tests, two-space indent, full if/else, "
                "#!/bin/bash shebang.",
            ),
        ]
    steps += [
        [
            (
                "edit_file",
                {
                    "path": OMARCHY_SCRIPT,
                    "old_text": by_path[OMARCHY_SCRIPT]["old_text"],
                    "new_text": by_path[OMARCHY_SCRIPT]["new_text"],
                },
            )
        ],
        [
            (
                "edit_file",
                {
                    "path": OMARCHY_TEST,
                    "old_text": by_path[OMARCHY_TEST]["old_text"],
                    "new_text": by_path[OMARCHY_TEST]["new_text"],
                },
            )
        ],
        [("run_tests", {"tests": ["test/shell"]})],
        [("diff", {})],
        [("submit_answer", {"answer": OMARCHY_SUMMARY})],
    ]
    return steps


def historical_theme_demo(task: CodingTask) -> list:
    """Evaluator-only reference replay, not an autonomous solution or forgetting prescription."""
    reference = task.spec_path.parent / "reference"  # type: ignore[union-attr]
    script = "bin/omarchy-theme-set"
    helper = "bin/omarchy-theme-colors-from-alacritty"
    return [
        [("read_file", {"path": script})],
        [("run_tests", {"tests": ["test/legacy-theme"]})],
        [("write_file", {"path": helper, "content": (reference / helper).read_text()})],
        [
            (
                "edit_file",
                {
                    "path": script,
                    "old_text": (task.snapshot_dir / script).read_text(),
                    "new_text": (reference / script).read_text(),
                },
            )
        ],
        [("run_tests", {"tests": ["test/legacy-theme"]})],
        [("diff", {})],
        [
            (
                "submit_answer",
                {
                    "answer": "Evaluator-only historical reference replay; "
                    "legacy palette compatibility passes the sandboxed visible suite."
                },
            )
        ],
    ]


def demo_script_for(task: CodingTask) -> list:
    """The method-arm walkthrough for a task (scripted fixture; not model behaviour)."""
    if task.task_id == "textstats":
        return coding_demo_script()
    if task.task_id == "omarchy-git-url-check":
        return omarchy_demo_script(task)
    if task.task_id == "omarchy-legacy-theme":
        return historical_theme_demo(task)
    if task.task_id == "ledger-workflow":
        from .ledger_workflow_demo import ledger_workflow_demo

        return ledger_workflow_demo(task)
    if task.task_id == "sqlite-csv":
        from .sqlite_csv_demo import sqlite_csv_demo

        return sqlite_csv_demo(task)
    raise ValueError(f"no offline walkthrough for task {task.task_id!r}; use --live")


def retained_script_for(task: CodingTask) -> list:
    """The walkthrough without context operations (retained and chronological arms)."""
    if task.task_id == "textstats":
        return coding_retained_script()
    if task.task_id == "omarchy-git-url-check":
        return omarchy_demo_script(task, retained=True)
    if task.task_id == "omarchy-legacy-theme":
        return historical_theme_demo(task)
    if task.task_id == "ledger-workflow":
        from .ledger_workflow_demo import ledger_workflow_demo

        return ledger_workflow_demo(task)
    if task.task_id == "sqlite-csv":
        from .sqlite_csv_demo import sqlite_csv_demo

        return sqlite_csv_demo(task)
    raise ValueError(f"no offline walkthrough for task {task.task_id!r}; use --live")


def coding_retained_script() -> list:
    return [
        [("list_files", {})],
        [("read_file", {"path": "README.md"})],
        [("read_file", {"path": "textstats/__init__.py"})],
        [("run_tests", {"tests": ["tests"]})],
        [("grep", {"pattern": 'split(" ")', "path_glob": "textstats/**"})],
        [
            (
                "edit_file",
                {"path": "textstats/__init__.py", "old_text": FIX_OLD, "new_text": FIX_NEW},
            )
        ],
        [("run_tests", {"tests": ["tests/test_textstats.py"]})],
        [("diff", {})],
        [("submit_answer", {"answer": FIX_SUMMARY})],
    ]


__all__ = [
    "CODING_PROMPT",
    "CodingTask",
    "coding_demo_script",
    "coding_retained_script",
    "demo_script_for",
    "grade",
    "load_coding_task",
    "retained_script_for",
    "run_coding_task",
]
