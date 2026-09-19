"""The explicit sequential episode loop.

One model, one task, one history. Each step builds the request from the active history, calls
the model once, stores the assistant message verbatim, executes the tool calls in emitted order
and appends one tool result per call. Termination states are explicit:

- submitted: `submit_answer` was called.
- step_cap: `max_calls` model calls were made without an answer.
- context_cap: the next request would exceed `max_context_chars` (a declared harness limit).
- invalid_termination: the assistant replied without any tool call.
- model_error: the provider exhausted retries, failed permanently, or returned malformed data.
- provider_attempt_cap: the separate physical request budget is exhausted. By default this
  equals max_calls, preserving the old HTTP safety bound even when retrying.

`request`/`response` remain logical turns. Additive `provider_attempt` events count real SDK
invocations, including failures. Only the provider retries, never tool execution.

Batch contract (documented limit of this first slice): a context-management tool
(context_apply, context_recover), or submit_answer, must be the ONLY tool call in its assistant
message. A message mixing one of them with any other call is rejected before any call in that
batch executes; every call receives the same rejection result so call/result pairing stays
valid. Ordinary task-tool batches execute in emitted order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .context import ContextState
from .media import input_metrics, project_messages
from .provider import OpenAIModel
from .tools import (
    CONTEXT_TOOL_NAMES,
    TOOL_SCHEMAS,
    ToolResult,
    format_call,
    parse_arguments,
    render_tools,
)
from .trace import Trace, json_size

RUNTIME_CONTEXT_THRESHOLD_TOKENS = 40_000
RUNTIME_CONTEXT_REMINDER_MIN_NEW_RESULT_CHARS = 20_000

PROMPTS_DIR = Path(__file__).parent / "prompts"
ARMS = ("method", "retained", "chronological")


class Model(Protocol):
    name: str

    def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...


class TaskTools(Protocol):
    """The task-side tools of one environment (corpus tools or coding workspace tools).

    `names` lists the tools in render order, `schemas` maps each name to its model-facing
    schema, and `call` executes one tool and returns a ToolResult (never raises for model
    mistakes). `submit_answer` and the context tools are added by the loop.
    """

    names: tuple[str, ...]
    schemas: dict[str, dict[str, Any]]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


@dataclass(frozen=True)
class SubmissionCriterion:
    """One immutable completion claim and the evidence expected for it."""

    id: str
    requirement: str
    evidence_requirements: str

    def __post_init__(self) -> None:
        for field_name in ("id", "requirement", "evidence_requirements"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"SubmissionCriterion.{field_name} must be nonempty")


@dataclass(frozen=True)
class LoopConfig:
    arm: str = "method"
    max_calls: int = 8  # logical turns; includes a failed logical request
    max_output_tokens: int = 2048
    max_context_chars: int | None = None  # request JSON chars; None = no cap
    clear_threshold_chars: int | None = None  # chronological arm only
    # Provider-specific request additions sent verbatim as `extra_body` (e.g. DeepSeek's
    # {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}). None = nothing added.
    extra_body: dict[str, Any] | None = None
    # Smoke path only: force the tool for step N via `tool_choice`. Empty = model chooses.
    forced_tools: tuple[str, ...] = ()
    max_provider_attempts: int | None = None  # None preserves max_calls physical safety cap
    # Optional task-defined completion gate. When nonempty, submit_answer must account for
    # every immutable criterion as verified or externally blocked with evidence. Generic
    # tasks keep (). Full semantics travel with the ID so a model cannot silently relabel it.
    required_submission_criteria: tuple[SubmissionCriterion, ...] = ()

    def __post_init__(self) -> None:
        if self.max_provider_attempts is not None and (
            type(self.max_provider_attempts) is not int or self.max_provider_attempts < 1
        ):
            raise ValueError("max_provider_attempts must be a positive integer")
        if any(
            not isinstance(item, SubmissionCriterion) for item in self.required_submission_criteria
        ):
            raise ValueError("required_submission_criteria must contain SubmissionCriterion")
        ids = [item.id for item in self.required_submission_criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("required_submission_criteria IDs must be unique")


@dataclass
class EpisodeResult:
    termination: str
    answer: str | None
    steps: int
    state: ContextState
    error: str | None = None


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def build_system_prompt(arm: str, shared: str = "task_shared.md") -> str:
    """Shared task section plus the method section only where the method tools exist.

    `shared` names the task-family prompt file (corpus question or coding task); it is
    identical across the comparison arms of one experiment.
    """
    shared_text = load_prompt(shared).replace(
        "{{context_checkpoint}}",
        load_prompt("coding_context_checkpoint.md") if arm == "method" else "",
    )
    parts = [shared_text]
    if arm == "method":
        parts.append(load_prompt("method_section.md"))
    return "\n\n".join(parts)


def prompt_fingerprint(arm: str, shared: str = "task_shared.md") -> dict[str, str]:
    text = build_system_prompt(arm, shared)
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "file": shared,
        "text": text,
    }


def tool_names_for(arm: str, tools: TaskTools) -> tuple[str, ...]:
    names = (*tools.names, "submit_answer")
    if arm == "method":
        return names + CONTEXT_TOOL_NAMES
    return names


def rendered_tools_for(
    arm: str,
    tools: TaskTools,
    required_submission_criteria: tuple[SubmissionCriterion, ...] = (),
) -> list[dict[str, Any]]:
    rendered = render_tools(tool_names_for(arm, tools), {**TOOL_SCHEMAS, **tools.schemas})
    if not required_submission_criteria:
        return rendered
    rendered = deepcopy(rendered)
    submit = next(t["function"] for t in rendered if t["function"]["name"] == "submit_answer")
    ids = [criterion.id for criterion in required_submission_criteria]
    listed = "\n".join(
        f"  - {criterion.id}\n"
        f"    Exact requirement: {criterion.requirement}\n"
        f"    Required evidence: {criterion.evidence_requirements}"
        for criterion in required_submission_criteria
    )
    submit["description"] += (
        "\n- This task has a backend completion gate. Account for every required criterion "
        "exactly once in completion_checks. Criterion IDs never rename or summarize their "
        "meaning. Use each exact requirement below:\n"
        f"{listed}"
    )
    submit["parameters"]["properties"]["completion_checks"] = {
        "type": "array",
        "description": (
            "One entry for every backend-required criterion. verified needs observed evidence; "
            "blocked needs a concrete external blocker. pending, partial and unverified work "
            "cannot be submitted."
        ),
        "minItems": len(required_submission_criteria),
        "maxItems": len(required_submission_criteria),
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "enum": ids},
                "criterion": {
                    "type": "string",
                    "enum": [criterion.requirement for criterion in required_submission_criteria],
                    "description": (
                        "Copy the exact requirement paired with this ID. The backend rejects "
                        "a requirement belonging to another ID or any rewritten meaning."
                    ),
                },
                "status": {"type": "string", "enum": ["verified", "blocked"]},
                "evidence": {
                    "type": "string",
                    "description": (
                        "Observed verification evidence or the concrete external blocker."
                    ),
                },
            },
            "required": ["id", "criterion", "status", "evidence"],
            "additionalProperties": False,
        },
    }
    submit["parameters"]["required"] = [
        *submit["parameters"]["required"],
        "completion_checks",
    ]
    return rendered


def run_episode(
    user_task: str,
    tools: TaskTools,
    model: Model,
    config: LoopConfig,
    trace: Trace,
    system_prompt: str | None = None,
    on_submission: Callable[[ContextState, str, int], str | None] | None = None,
    initial_state: ContextState | None = None,
) -> EpisodeResult:
    if config.arm not in ARMS:
        raise ValueError(f"unknown arm {config.arm!r}; expected one of {ARMS}")
    if initial_state is None:
        state = ContextState(system_prompt or build_system_prompt(config.arm), user_task)
    else:
        # Branch only completed sessions; preserve source history, IDs, reasoning and archive.
        last = initial_state.messages[-1]
        record = initial_state.records.get(last.get("tool_call_id"))
        if last.get("role") != "tool" or record is None or record.kind != "answer":
            raise ValueError("A branch requires a completed submission")
        if not isinstance(user_task, str) or not user_task.strip():
            raise ValueError("A continuation must be a nonempty user message")
        if config.arm == "retained" and (initial_state.stubs or initial_state.cleared):
            raise ValueError("A retained branch requires an unpruned source history")
        state = deepcopy(initial_state)
        if system_prompt is not None:
            state.messages[0] = {"role": "system", "content": system_prompt}
        state.messages.append({"role": "user", "content": user_task})
        state.reset_runtime_notice()
        trace.event(
            "session_branch",
            inherited_messages=len(initial_state.messages),
            inherited_steps=initial_state.step,
            system_prompt_changed=(state.messages[0] != initial_state.messages[0]),
        )
        trace.event("user_message", step=0, content=user_task)
    task_tools = tools
    tools = rendered_tools_for(config.arm, task_tools, config.required_submission_criteria)
    answer: str | None = None
    steps = 0
    physical_attempts = 0
    physical_cap = config.max_provider_attempts
    if physical_cap is None:
        physical_cap = config.max_calls

    def record_attempt(event: dict[str, Any]) -> None:
        nonlocal physical_attempts
        physical_attempts += 1
        trace.event("provider_attempt", step=steps, physical_attempt=physical_attempts, **event)

    while steps < config.max_calls:
        if physical_attempts >= physical_cap:
            trace.event(
                "termination",
                reason="provider_attempt_cap",
                steps=steps,
                provider_attempts=physical_attempts,
            )
            return EpisodeResult("provider_attempt_cap", None, steps, state)
        if config.arm == "chronological" and config.clear_threshold_chars is not None:
            _clear_chronologically(state, config, tools, model, trace)
        request = _build_request(state, tools, model.name, config)
        if steps < len(config.forced_tools):
            forced = config.forced_tools[steps]
            request["tool_choice"] = {"type": "function", "function": {"name": forced}}
        if isinstance(model, OpenAIModel):
            request = model.prepare_request(request)
        chars, nbytes = json_size(request)
        if config.max_context_chars is not None and chars > config.max_context_chars:
            trace.event("termination", reason="context_cap", request_chars=chars, steps=steps)
            return EpisodeResult("context_cap", None, steps, state)
        if _has_runtime_context(request["messages"]):
            state.mark_runtime_notice_sent()

        steps += 1
        trace.event(
            "request",
            step=steps,
            chars=chars,
            bytes=nbytes,
            body=request,
            input_metrics=input_metrics(request["messages"], request["tools"]),
        )
        response = _call_model(
            model,
            request,
            trace,
            steps,
            attempt_limit=physical_cap - physical_attempts,
            on_attempt=record_attempt,
        )
        if not isinstance(model, OpenAIModel):
            physical_attempts += 1  # scripted/custom adapters remain single-call, no retries

        if isinstance(response, str):
            reason = "model_error"
            if (
                isinstance(model, OpenAIModel)
                and model.last_attempts
                and (model.last_attempts[-1]["stop_reason"] == "physical_request_cap")
            ):
                reason = "provider_attempt_cap"
            trace.event(
                "termination",
                reason=reason,
                error=response,
                steps=steps,
                provider_attempts=physical_attempts,
            )
            return EpisodeResult(reason, None, steps, state, error=response)
        trace.event("response", step=steps, body=response, usage=response.get("usage"))

        message = dict(response["choices"][0]["message"])
        message.setdefault("role", "assistant")
        call_ids = [call["id"] for call in message.get("tool_calls") or []]
        if len(call_ids) != len(set(call_ids)) or any(cid in state.records for cid in call_ids):
            error = "Duplicate provider tool-call ID; refusing ambiguous result provenance"
            trace.event("termination", reason="model_error", error=error, steps=steps)
            return EpisodeResult("model_error", None, steps, state, error=error)
        state.append_assistant(message)
        calls = message.get("tool_calls") or []
        if not calls:
            trace.event("termination", reason="invalid_termination", steps=steps)
            return EpisodeResult("invalid_termination", None, steps, state)

        for call, result, kind in _execute_batch(calls, state, task_tools, config, trace, steps):
            arguments = parse_arguments(call["function"]["arguments"])
            state.append_tool_result(
                call["id"],
                call["function"]["name"],
                arguments if isinstance(arguments, dict) else {},
                result.text,
                kind=kind,
                images=result.images,
            )
            if kind == "answer":
                answer = arguments.get("answer") if isinstance(arguments, dict) else None
        if answer is not None:
            if on_submission is not None:
                next_task = on_submission(state, answer, steps)
                if next_task is not None:
                    if not isinstance(next_task, str) or not next_task.strip():
                        raise ValueError("A continuation must be a nonempty user message")
                    state.messages.append({"role": "user", "content": next_task})
                    state.reset_runtime_notice()
                    trace.event("user_message", step=steps, content=next_task)
                    answer = None
                    continue
            trace.event("termination", reason="submitted", answer=answer, steps=steps)
            return EpisodeResult("submitted", answer, steps, state)

    trace.event("termination", reason="step_cap", steps=steps)
    return EpisodeResult("step_cap", None, steps, state)


def _build_request(
    state: ContextState,
    tools: list[dict[str, Any]],
    model_name: str,
    config: LoopConfig,
) -> dict[str, Any]:
    messages = project_messages(state.messages)
    request: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "tools": tools,
        "max_tokens": config.max_output_tokens,
    }
    if config.arm == "method":
        # Recompute from the current history, including tool output and context edits.
        # This is an explicit estimate, not a model-specific tokenizer measurement.
        metrics = input_metrics(messages, tools)
        estimated_tokens = metrics["estimated_text_tokens"]
        threshold_reached = (
            estimated_tokens >= RUNTIME_CONTEXT_THRESHOLD_TOKENS or metrics["image_count"]
        )
        if threshold_reached and state.runtime_notice_due(
            RUNTIME_CONTEXT_REMINDER_MIN_NEW_RESULT_CHARS
        ):
            messages.append(
                {
                    "role": "user",
                    "content": load_prompt(
                        "runtime_context_images.md"
                        if metrics["image_count"]
                        else "runtime_context.md"
                    ).format(image_count=metrics["image_count"]),
                }
            )
    if config.extra_body:
        request["extra_body"] = dict(config.extra_body)
    return request


def _has_runtime_context(messages: list[dict[str, Any]]) -> bool:
    if not messages:
        return False
    content = messages[-1].get("content")
    return isinstance(content, str) and content.startswith("<runtime_context>")


def _call_model(
    model: Model,
    request: dict[str, Any],
    trace: Trace,
    step: int,
    *,
    attempt_limit: int,
    on_attempt: Callable[[dict[str, Any]], None],
) -> dict[str, Any] | str:
    """Make one logical call (bounded provider attempts); return response or error text.

    A raised exception and a malformed response body are both recorded as `model_error`
    events (the malformed body is kept in the event) and end the episode.
    """
    try:
        if isinstance(model, OpenAIModel):
            response = model.complete(
                request,
                attempt_limit=attempt_limit,
                on_attempt=on_attempt,
                on_chunk=lambda event: trace.event("provider_chunk", step=step, **event),
            )
        else:
            response = model.complete(request)
    except Exception as error:  # noqa: BLE001 - every provider failure ends the episode
        # SDK error strings can echo response bodies; attempt metadata is sufficient here.
        text = (
            type(error).__name__
            if isinstance(model, OpenAIModel)
            else (f"{type(error).__name__}: {error}")
        )
        trace.event("model_error", step=step, error=text)
        return text
    problem = _response_problem(response)
    if problem is not None:
        text = f"malformed response: {problem}"
        trace.event("model_error", step=step, error=text, body=response)
        return text
    return response


def _response_problem(response: Any) -> str | None:
    """Why `response` cannot be used as an OpenAI chat completion, or None if it can."""
    if not isinstance(response, dict):
        return f"body is {type(response).__name__}, not an object"
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return "no choices"
    if len(choices) != 1:
        return "expected exactly one completion choice"
    choice = choices[0]
    if isinstance(choice, dict) and "finish_reason" in choice:
        reason = choice["finish_reason"]
        if reason not in ("stop", "tool_calls"):
            return f"incomplete completion: finish_reason={reason!r}"
        has_calls = (
            bool((choice.get("message") or {}).get("tool_calls"))
            if isinstance(choice.get("message"), dict)
            else False
        )
        if has_calls != (reason == "tool_calls"):
            return "finish_reason does not match tool calls"
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return "first choice has no message object"
    if message.get("role", "assistant") != "assistant":
        return "completion message role must be assistant"
    if not isinstance(message.get("content"), str | type(None)):
        return "completion content must be text or null"
    calls = message.get("tool_calls")
    if calls is None:
        return None
    if not isinstance(calls, list):
        return "tool_calls is not a list"
    for index, call in enumerate(calls):
        function = call.get("function") if isinstance(call, dict) else None
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("id"), str)
            or not call["id"]
            or call.get("type", "function") != "function"
            or not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"]
            or not isinstance(function.get("arguments"), str | dict | type(None))
        ):
            return f"tool_calls[{index}] lacks a string id, function.name or arguments"
    return None


def _execute_batch(
    calls: list[dict[str, Any]],
    state: ContextState,
    tools: TaskTools,
    config: LoopConfig,
    trace: Trace,
    step: int,
) -> list[tuple[dict[str, Any], ToolResult, str]]:
    """Execute one assistant message's calls in order, or reject the whole batch."""
    names = [call["function"]["name"] for call in calls]
    mixed = len(calls) > 1 and any(name in (*CONTEXT_TOOL_NAMES, "submit_answer") for name in names)
    if mixed:
        offenders = ", ".join(
            sorted({n for n in names if n in (*CONTEXT_TOOL_NAMES, "submit_answer")})
        )
        text = (
            f"Batch rejected: {offenders} must be the only tool call in a message. None of "
            f"the {len(calls)} calls in this message was executed. Re-issue them one message "
            "at a time."
        )
        trace.event("batch_rejected", step=step, tools=names)
        return [(call, ToolResult(text, ok=False), "error") for call in calls]

    outcomes = []
    for call in calls:
        name = call["function"]["name"]
        arguments = parse_arguments(call["function"]["arguments"])
        trace.event("tool_call", step=step, id=call["id"], name=name, arguments=arguments)
        if isinstance(arguments, ToolResult):
            result, kind = arguments, "error"
        else:
            result, kind = _dispatch(name, arguments, call["id"], state, tools, config, trace)
        trace.event(
            "tool_result",
            step=step,
            id=call["id"],
            ok=result.ok,
            kind=kind,
            text=result.text,
            **({"images": [asdict(image) for image in result.images]} if result.images else {}),
        )
        outcomes.append((call, result, kind))
    return outcomes


def _dispatch(
    name: str,
    arguments: dict[str, Any],
    call_id: str,
    state: ContextState,
    tools: TaskTools,
    config: LoopConfig,
    trace: Trace,
) -> tuple[ToolResult, str]:
    available = tool_names_for(config.arm, tools)
    if name not in available:
        return (
            ToolResult(
                f"Tool {name!r} is not available in this run. Available: {', '.join(available)}.",
                ok=False,
            ),
            "error",
        )
    if name in tools.names:
        return tools.call(name, arguments), "task"
    if name == "submit_answer":
        answer = arguments.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return ToolResult("submit_answer needs a non-empty 'answer' string.", ok=False), "error"
        problem = _submission_gate_problem(arguments, config.required_submission_criteria)
        if problem is not None:
            return ToolResult(problem, ok=False), "error"
        return ToolResult("Submission recorded for the current user request."), "answer"
    if name == "context_apply":
        before_chars, _ = json_size(state.messages)
        before_count = len(state.messages)
        result = state.apply(arguments.get("targets"))
        after_chars, _ = json_size(state.messages)
        trace.event(
            "context_transition",
            kind="apply",
            ok=result.ok,
            messages_before=before_count,
            messages_after=len(state.messages),
            history_chars_before=before_chars,
            history_chars_after=after_chars,
            stubs={
                ref: format_call(state.records[tid].name, state.records[tid].arguments)
                for tid, ref in state.stubs.items()
            },
            result=result.text,
        )
        return result, ("apply" if result.ok else "error")
    if name == "context_recover":
        ref = str(arguments.get("ref", ""))
        result = state.recover(ref)
        trace.event("recovery", ref=ref, ok=result.ok, chars=len(result.text))
        return result, ("recovered" if result.ok else "error")
    return ToolResult(f"Unknown tool {name!r}.", ok=False), "error"


def _submission_gate_problem(
    arguments: dict[str, Any], required: tuple[SubmissionCriterion, ...]
) -> str | None:
    """Return an actionable rejection for a configured completion checklist."""
    if not required:
        return None
    required_by_id = {criterion.id: criterion for criterion in required}
    checks = arguments.get("completion_checks")
    if not isinstance(checks, list):
        return (
            "Submission rejected: completion_checks must account for every required "
            f"criterion exactly once: {', '.join(required_by_id)}. Continue the task and retry."
        )
    seen: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for index, check in enumerate(checks, start=1):
        if not isinstance(check, dict):
            problems.append(f"entry {index} is not an object")
            continue
        criterion = check.get("id")
        if criterion not in required_by_id:
            problems.append(f"entry {index} has unknown id {criterion!r}")
            continue
        if criterion in seen:
            problems.append(f"criterion {criterion!r} appears more than once")
            continue
        seen[criterion] = check
        supplied_requirement = check.get("criterion")
        exact_requirement = required_by_id[criterion].requirement
        if supplied_requirement != exact_requirement:
            problems.append(
                f"criterion {criterion!r} was relabelled; copy its exact requirement unchanged"
            )
        status = check.get("status")
        if status not in {"verified", "blocked"}:
            problems.append(
                f"criterion {criterion!r} has status {status!r}; use verified or blocked"
            )
        evidence = check.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            problems.append(f"criterion {criterion!r} needs concrete evidence")
    missing = [criterion.id for criterion in required if criterion.id not in seen]
    if missing:
        problems.append(f"missing required criteria: {', '.join(missing)}")
    if not problems:
        return None
    return (
        "Submission rejected: "
        + "; ".join(problems)
        + ". No submission was recorded. Complete or verify the missing work; use blocked only "
        "for a concrete external blocker, then retry submit_answer."
    )


def _clear_chronologically(
    state: ContextState,
    config: LoopConfig,
    tools: list[dict[str, Any]],
    model: Model,
    trace: Trace,
) -> None:
    """Baseline arm: clear the oldest task results (not recoverable) until under threshold."""
    threshold = config.clear_threshold_chars
    assert threshold is not None
    while True:
        request = _build_request(state, tools, model.name, config)
        if isinstance(model, OpenAIModel):
            request = model.prepare_request(request)
        chars, _ = json_size(request)
        if chars <= threshold:
            return
        cleared = state.clear_oldest_result()
        if cleared is None:
            return
        trace.event(
            "context_transition", kind="chronological_clear", cleared=cleared, chars_before=chars
        )


__all__ = [
    "ARMS",
    "EpisodeResult",
    "LoopConfig",
    "Model",
    "SubmissionCriterion",
    "TaskTools",
    "run_episode",
    "build_system_prompt",
    "prompt_fingerprint",
    "rendered_tools_for",
    "tool_names_for",
    "json",
]
