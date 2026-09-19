"""Controlled provider contract smoke path.

PRESCRIBED, not autonomous: the harness forces the tool of every step through `tool_choice`
and the user message spells out the arguments. What the smoke path establishes is whether the
provider accepts (a) an assistant message with its own `reasoning_content` sent back during a
tool turn, (b) an edited tool result (stub) with stable call/result pairing, and (c) a recovery
appended at the end. It says nothing about whether a model would choose these
operations on its own. Argument choice (path, ID, note) is still the model's; the checker
reports whether the prescription was followed.

Offline, the same plan is followed by the scripted fixture to self-test the checker. Verdicts
are derived only from observed successful operations: an absent observation is never a pass.
`passed` requires every check to pass; `failed` means at least one check failed;
`inconclusive` means nothing failed but something could not be observed (no reasoning_content
came back, or the run used the fixture, which cannot show provider acceptance).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .experiment import Task, load_task, run_task
from .loop import LoopConfig, Model
from .scripted import ScriptedModel, result_id_for_path

SMOKE_PLAN: tuple[str, ...] = (
    "read_file",
    "read_file",
    "context_apply",
    "context_recover",
    "submit_answer",
)

SMOKE_INSTRUCTIONS = (
    "This is a harness contract check, not a research task. Make exactly one tool call per "
    "reply, in this order, with these arguments:\n"
    "1. read_file with path 'regulations-excerpt.md'.\n"
    "2. read_file with path 'site-survey.md'.\n"
    "3. context_apply with one target: copy the id from the context_result envelope of "
    "the regulations-excerpt.md output, "
    "note 'contract check: clause 4.3.2 sets 600 mm freeboard'.\n"
    "4. context_recover with the ref shown in the stub that replaced that result.\n"
    "5. submit_answer with answer 'smoke complete'.\n"
    "Do not add other calls."
)


FIXTURE_MODE = "scripted-fixture"
VERDICT_EXIT_CODES = {"passed": 0, "failed": 1, "inconclusive": 2}


def smoke_task() -> Task:
    base = load_task("riverside")
    return Task(
        task_id="smoke-contract",
        question=SMOKE_INSTRUCTIONS,
        corpus_root=base.corpus_root,
        gold_answer="smoke complete",
        answer_pattern="smoke complete",
        notes="Prescribed provider contract smoke; not a task result.",
    )


def scripted_follower() -> ScriptedModel:
    """Fixture that follows SMOKE_PLAN, so the checker can be tested offline."""

    def apply_step(messages: list[dict[str, Any]]) -> list:
        result_id = result_id_for_path(messages, "regulations-excerpt.md")
        note = "contract check: clause 4.3.2 sets 600 mm freeboard"
        return [("context_apply", {"targets": [{"id": result_id, "note": note}]})]

    def recover_step(messages: list[dict[str, Any]]) -> list:
        stub = next(
            m["content"] for m in messages if (m.get("content") or "").startswith("[set aside")
        )
        return [("context_recover", {"ref": stub.rsplit("ref:", 1)[1].rstrip("]")})]

    return ScriptedModel(
        [
            [("read_file", {"path": "regulations-excerpt.md"})],
            [("read_file", {"path": "site-survey.md"})],
            apply_step,
            recover_step,
            [("submit_answer", {"answer": "smoke complete"})],
        ]
    )


def run_smoke(
    model: Model,
    runs_dir: Path,
    session_id: str,
    mode: str,
    max_calls: int | None = None,
    max_output_tokens: int = 2048,
    extra_body: dict[str, Any] | None = None,
    provider_info: dict[str, Any] | None = None,
    max_provider_attempts: int | None = None,
) -> dict[str, Any]:
    """Run the prescribed plan and write `smoke-report.json` with checks and a verdict.

    `max_calls` must cover the plan; the CLI validates this before any provider exists.
    """
    calls = len(SMOKE_PLAN) if max_calls is None else max_calls
    if calls < len(SMOKE_PLAN):
        raise ValueError(f"smoke needs at least {len(SMOKE_PLAN)} calls, got {calls}")
    config = LoopConfig(
        arm="method",
        max_calls=calls,
        max_provider_attempts=max_provider_attempts,
        max_output_tokens=max_output_tokens,
        extra_body=extra_body,
        forced_tools=SMOKE_PLAN,
    )
    summary = run_task(
        smoke_task(),
        model,
        config,
        runs_dir=runs_dir,
        session_id=session_id,
        provider_info=provider_info,
    )
    run_dir = Path(summary["run_dir"])
    events = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    checks = check_contract(events, mode=mode)
    report = {
        "mode": mode,
        "session_id": session_id,
        "prescribed_tools": list(SMOKE_PLAN),
        "extra_body": extra_body,
        "termination": summary["termination"],
        "verdict": verdict_for(checks),
        "checks": checks,
    }
    (run_dir / "smoke-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def verdict_for(checks: list[dict[str, Any]]) -> str:
    statuses = {c["status"] for c in checks}
    if "fail" in statuses:
        return "failed"
    if "inconclusive" in statuses:
        return "inconclusive"
    return "passed"


def check_contract(events: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """Derive contract observations from the trace. Statuses: pass, fail, inconclusive.

    Every structural check needs the prescribed operation to have SUCCEEDED and the next
    request to have been recorded; otherwise it fails. Only two things are inconclusive rather
    than failed: no response carried `reasoning_content` (the roundtrip cannot be observed),
    and fixture mode (no provider was called, so acceptance cannot be claimed).
    """
    requests = [e for e in events if e["event"] == "request"]
    responses = [e for e in events if e["event"] == "response"]
    errors = [e for e in events if e["event"] == "model_error"]
    termination = next((e for e in events if e["event"] == "termination"), {})
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # (a) reasoning roundtrip: every response with reasoning must be echoed verbatim next time
    with_reasoning = [
        r for r in responses if r["body"]["choices"][0]["message"].get("reasoning_content")
    ]
    if not responses:
        add("reasoning_roundtrip", "fail", "no response recorded")
    elif not with_reasoning:
        add(
            "reasoning_roundtrip",
            "inconclusive",
            f"none of {len(responses)} responses carried reasoning_content; enable thinking "
            "(--thinking enabled) to observe the roundtrip",
        )
    else:
        problems = []
        for response in with_reasoning:
            reasoning = response["body"]["choices"][0]["message"]["reasoning_content"]
            following = _next_request(events, response["seq"])
            if following is None:
                if response is not responses[-1]:
                    problems.append(f"step {response['step']}: no request followed")
                continue
            echoed = any(
                m.get("role") == "assistant" and m.get("reasoning_content") == reasoning
                for m in following["body"]["messages"]
            )
            if not echoed:
                problems.append(f"step {response['step']}: reasoning missing or altered")
        add(
            "reasoning_roundtrip",
            "fail" if problems else "pass",
            "; ".join(problems)
            if problems
            else f"{len(with_reasoning)} response(s) with reasoning_content echoed verbatim "
            "in the following request",
        )

    # (b) stub present in the request after a successful apply
    apply_events = [
        e
        for e in events
        if e["event"] == "context_transition" and e.get("kind") == "apply" and e.get("ok")
    ]
    following = _next_request(events, apply_events[0]["seq"]) if apply_events else None
    if not apply_events:
        add("stub_in_request_after_apply", "fail", "no successful context_apply happened")
    elif following is None:
        add("stub_in_request_after_apply", "fail", "no request after apply")
    else:
        messages = following["body"]["messages"]
        has_stub = any(_is_stub(m) for m in messages)
        add(
            "stub_in_request_after_apply",
            "pass" if has_stub else "fail",
            "stub replaces the original tool result in place" if has_stub else "no stub found",
        )
    # (c) recovery appended at the end, old stub kept
    recoveries = [e for e in events if e["event"] == "recovery" and e.get("ok")]
    following = _next_request(events, recoveries[0]["seq"]) if recoveries else None
    if not recoveries:
        add("recovery_appended_at_end", "fail", "no successful context_recover happened")
        add("old_stub_kept_after_recovery", "fail", "no successful context_recover happened")
    elif following is None:
        add("recovery_appended_at_end", "fail", "no request after recovery")
        add("old_stub_kept_after_recovery", "fail", "no request after recovery")
    else:
        messages = following["body"]["messages"]
        last = messages[-1]
        at_end = last.get("role") == "tool" and "\nRecovered ic-" in str(last.get("content", ""))
        add(
            "recovery_appended_at_end",
            "pass" if at_end else "fail",
            "recovered text is the newest tool result"
            if at_end
            else "last message is not the recovery",
        )
        stub_kept = any(_is_stub(m) for m in messages[:-1])
        add(
            "old_stub_kept_after_recovery",
            "pass" if stub_kept else "fail",
            "earlier stub unchanged" if stub_kept else "stub disappeared",
        )

    # provider acceptance: only a real provider can show it
    if mode == FIXTURE_MODE:
        add(
            "provider_accepted_every_request",
            "inconclusive",
            "scripted fixture: no provider was called, acceptance cannot be claimed",
        )
    else:
        accepted = (
            bool(requests)
            and len(requests) == len(responses)
            and not errors
            and termination.get("reason") != "model_error"
        )
        add(
            "provider_accepted_every_request",
            "pass" if accepted else "fail",
            f"{len(responses)} responses for {len(requests)} requests, {len(errors)} model "
            f"errors, termination {termination.get('reason')}",
        )

    # prescription adherence: the right tools in the right order, every one successful
    called = [e["name"] for e in events if e["event"] == "tool_call"]
    failed_results = [
        f"step {e['step']}: {e['text'][:80]}"
        for e in events
        if e["event"] == "tool_result" and not e.get("ok")
    ]
    problems = []
    if called != list(SMOKE_PLAN):
        problems.append(f"tool calls made: {called}")
    if failed_results:
        problems.append("failed tool results: " + " | ".join(failed_results))
    if termination.get("reason") != "submitted":
        problems.append(f"termination {termination.get('reason')}")
    add(
        "prescribed_sequence_followed",
        "fail" if problems else "pass",
        "; ".join(problems)
        if problems
        else f"tool calls made in order with successful results: {called}",
    )
    return checks


def _is_stub(message: dict[str, Any]) -> bool:
    return message.get("role") == "tool" and str(message.get("content", "")).startswith(
        "[set aside"
    )


def _next_request(events: list[dict[str, Any]], after_seq: int) -> dict[str, Any] | None:
    return next((e for e in events if e["event"] == "request" and e["seq"] > after_seq), None)
