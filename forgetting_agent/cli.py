"""Command-line entry point.

    forgetting-agent run   [--arm method|retained|chronological|all] [--live ...]
    forgetting-agent code  [--arm ...] [--task <coding task>] [--test-timeout S] [--live ...]
    forgetting-agent smoke [--live ...]

Without `--live` everything runs offline with the SCRIPTED fixture (prescribed steps that
exercise the harness; not model behaviour). `--live` loads TENSORX_* from the explicit .env
file (environment overrides win), calls the provider through the OpenAI SDK with the given
caps and timeout, and writes the same trace files. Every run prints its session ID; a session
ID is used once (an existing run directory is refused, there is no resume).

Exit codes: 0 success / smoke passed, 1 smoke failed, 2 usage or setup error / smoke
inconclusive.

`code` runs a coding task (workspace tools plus sandboxed run_tests, trusted parent-side
grader after the episode). It requires the sandbox preflight to pass; otherwise it is refused
with exit 2 and the failing check, before any run directory exists. Task code never runs on
the host.

Visible reasoning is provider-specific. `--thinking enabled|disabled` resolves to TensorX's
per-family boolean inside `extra_body.chat_template_kwargs` for the configured `TENSORX_MODEL`
(DeepSeek V4: `thinking`, GLM: `enable_thinking`; see `provider.THINKING_KEYS`). A model
outside the mapped families, and the offline fixture, are refused with exit 2 before anything
is written. `--reasoning-effort <level>` adds the top-level `reasoning_effort`. The trace
records exactly what was sent; whether the provider honours it is checked, not assumed.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import sys
from pathlib import Path
from typing import Any

from .coding import demo_script_for, load_coding_task, retained_script_for, run_coding_task
from .costs import fetch_pricing
from .experiment import (
    load_task,
    method_demo_script,
    new_session_id,
    retained_demo_script,
    run_task,
)
from .loop import ARMS, LoopConfig
from .provider import (
    OpenAIModel,
    Settings,
    documented_max_output_tokens,
    load_settings,
    thinking_extra_body,
)
from .sandbox import Sandbox, preflight
from .scripted import ScriptedModel
from .smoke import FIXTURE_MODE, SMOKE_PLAN, VERDICT_EXIT_CODES, run_smoke, scripted_follower

OFFLINE_BANNER = (
    "OFFLINE DEMO with the SCRIPTED fixture: prescribed tool calls exercise the harness; "
    "this is not model behaviour and not an efficacy result."
)
DEFAULT_MAX_CALLS_LIVE = 8
DEFAULT_MAX_CALLS_OFFLINE = 12


class UsageError(RuntimeError):
    """Rejected before any provider object exists or any file is written."""


def positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text}")
    return value


def positive_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive finite number, got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forgetting-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the task in one arm (offline fixture unless --live)")
    run.add_argument("--arm", choices=(*ARMS, "all"), default="method")
    run.add_argument("--task", default="riverside")
    _add_common(run)
    run.add_argument(
        "--max-context-chars",
        type=positive_int,
        default=None,
        help="terminate with context_cap when a request exceeds this many JSON chars",
    )
    run.add_argument(
        "--clear-threshold",
        type=positive_int,
        default=6000,
        help="chronological arm: clear oldest results above this many request chars",
    )

    code = sub.add_parser(
        "code", help="run a coding task in the sandbox (offline fixture unless --live)"
    )
    code.add_argument("--arm", choices=(*ARMS, "all"), default="method")
    code.add_argument("--task", default="textstats")
    _add_common(code)
    code.add_argument(
        "--max-context-chars",
        type=positive_int,
        default=None,
        help="terminate with context_cap when a request exceeds this many JSON chars",
    )
    code.add_argument(
        "--clear-threshold",
        type=positive_int,
        default=6000,
        help="chronological arm: clear oldest results above this many request chars",
    )
    code.add_argument(
        "--test-timeout",
        type=positive_float,
        default=None,
        help="seconds per run_tests / grader execution (default: the task's test_timeout_s)",
    )

    smoke = sub.add_parser(
        "smoke", help="prescribed provider contract smoke (fixture unless --live)"
    )
    _add_common(smoke)
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--live", action="store_true", help="call the real provider (paid)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="dotenv file for --live")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--session-id", default=None, help="used once; existing dirs are refused")
    parser.add_argument(
        "--max-calls",
        type=positive_int,
        default=None,
        help=(
            f"logical model turns per episode (run: default {DEFAULT_MAX_CALLS_LIVE} live, "
            f"{DEFAULT_MAX_CALLS_OFFLINE} offline; smoke: default and minimum {len(SMOKE_PLAN)})"
        ),
    )
    parser.add_argument(
        "--max-provider-attempts",
        type=positive_int,
        default=None,
        help="total physical requests per episode INCLUDING retries; default: "
        "max-calls (no silent budget increase)",
    )
    parser.add_argument(
        "--provider-max-attempts",
        type=positive_int,
        default=6,
        help="attempts per logical request, including first (default: 3)",
    )
    parser.add_argument(
        "--provider-backoff-cap",
        type=positive_float,
        default=60.0,
        help="maximum retry wait in seconds, including Retry-After (default: 8)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=positive_int,
        default=None,
        help=(
            "per-request output allowance; live GLM-5.3 defaults to its documented maximum, "
            "other live models require an explicit value"
        ),
    )
    parser.add_argument(
        "--timeout", type=positive_float, default=120.0, help="SDK timeout in seconds"
    )
    parser.add_argument(
        "--thinking",
        choices=("enabled", "disabled"),
        default=None,
        help=(
            "send TensorX's per-family extra_body.chat_template_kwargs toggle for the configured "
            "model (DeepSeek V4: thinking, GLM: enable_thinking); other models are refused"
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "high", "max"),
        default=None,
        help=(
            "send top-level reasoning_effort (GLM 5.3/Flash: low, high, max; "
            "omitted leaves provider default; TensorX effective routing is not verified)"
        ),
    )


def extra_body_from(args: argparse.Namespace, model: str) -> dict[str, Any] | None:
    """The exact provider extras implied by the flags for `model`, or None without flags.

    `--thinking` is resolved through the per-family TensorX key; an unmapped model (including
    the offline fixture) raises ValueError before anything is written or called.
    """
    extras: dict[str, Any] = {}
    if args.thinking is not None:
        extras.update(thinking_extra_body(model, args.thinking == "enabled"))
    if args.reasoning_effort is not None:
        extras["reasoning_effort"] = args.reasoning_effort
    return extras or None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = args.session_id or new_session_id()
    try:
        if args.command == "run":
            return _run(args, session)
        if args.command == "code":
            return _code(args, session)
        return _smoke(args, session)
    except (RuntimeError, FileExistsError, ValueError) as error:
        # Missing live settings (values are never printed), refused run directory, bad caps.
        print(f"error: {error}", file=sys.stderr)
        return 2


def _refuse_existing(runs_dir: Path, session_ids: list[str]) -> None:
    taken = [s for s in session_ids if (Path(runs_dir) / s).exists()]
    if taken:
        raise UsageError(
            f"run directory already exists for session {', '.join(taken)} in {runs_dir}; "
            "a session ID is used once and there is no resume. Choose a new --session-id."
        )


def _live_model(args: argparse.Namespace, settings: Settings) -> OpenAIModel:
    model = OpenAIModel(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.model,
        timeout=args.timeout,
        max_attempts=args.provider_max_attempts,
        backoff_cap=args.provider_backoff_cap,
    )
    model.pricing = fetch_pricing(settings.base_url, settings.api_key, settings.model)
    return model


def _output_budget(args: argparse.Namespace, model: str | None = None) -> int:
    if args.max_output_tokens is not None:
        return args.max_output_tokens
    if model is None:
        return LoopConfig().max_output_tokens
    try:
        return documented_max_output_tokens(model)
    except ValueError as exc:
        raise UsageError(f"{exc}; pass --max-output-tokens explicitly") from exc


def _run(args: argparse.Namespace, session: str) -> int:
    task = load_task(args.task)
    arms = list(ARMS) if args.arm == "all" else [args.arm]
    session_ids = [f"{session}-{arm}" if len(arms) > 1 else session for arm in arms]
    _refuse_existing(args.runs_dir, session_ids)
    if args.max_calls is None:
        max_calls = DEFAULT_MAX_CALLS_LIVE if args.live else DEFAULT_MAX_CALLS_OFFLINE
    else:
        max_calls = args.max_calls
    if args.live:
        settings = load_settings(args.env)
        extra_body = extra_body_from(args, settings.model)
        max_output_tokens = _output_budget(args, settings.model)
        model = _live_model(args, settings)
        provider_info = model.describe()
        print(
            f"LIVE run against {provider_info['base_url']} model {model.name}; "
            f"caps: {max_calls} calls, {max_output_tokens} output tokens, "
            f"timeout {args.timeout}s; extra_body: {extra_body}"
        )
    else:
        max_output_tokens = _output_budget(args)
        extra_body = extra_body_from(args, ScriptedModel.name)
        print(OFFLINE_BANNER)
        provider_info = None
    print(f"task {task.task_id}: {task.notes}")

    for arm, session_id in zip(arms, session_ids, strict=True):
        config = LoopConfig(
            arm=arm,
            max_calls=max_calls,
            max_provider_attempts=args.max_provider_attempts,
            max_output_tokens=max_output_tokens,
            max_context_chars=args.max_context_chars,
            clear_threshold_chars=args.clear_threshold if arm == "chronological" else None,
            extra_body=extra_body,
        )
        if args.live:
            arm_model = model
        else:
            script = method_demo_script() if arm == "method" else retained_demo_script()
            arm_model = ScriptedModel(script)
        summary = run_task(
            task,
            arm_model,
            config,
            runs_dir=args.runs_dir,
            session_id=session_id,
            provider_info=provider_info,
        )
        _print_summary(arm, summary)
    return 0


def _code(args: argparse.Namespace, session: str) -> int:
    report = preflight()
    if not report.ok:
        raise UsageError(
            "coding mode is blocked: the sandbox preflight failed and task code is never "
            f"executed on the host. Checks: {report.summary()}"
        )
    task = load_coding_task(args.task)
    if args.test_timeout is not None:
        task = dataclasses.replace(task, test_timeout_s=args.test_timeout)
    arms = list(ARMS) if args.arm == "all" else [args.arm]
    session_ids = [f"{session}-{arm}" if len(arms) > 1 else session for arm in arms]
    _refuse_existing(args.runs_dir, session_ids)
    if args.max_calls is None:
        max_calls = DEFAULT_MAX_CALLS_LIVE if args.live else DEFAULT_MAX_CALLS_OFFLINE
    else:
        max_calls = args.max_calls
    if args.live:
        settings = load_settings(args.env)
        extra_body = extra_body_from(args, settings.model)
        max_output_tokens = _output_budget(args, settings.model)
        model = _live_model(args, settings)
        provider_info = model.describe()
        print(
            f"LIVE coding run against {provider_info['base_url']} model {model.name}; "
            f"caps: {max_calls} calls, {max_output_tokens} output tokens, "
            f"timeout {args.timeout}s, test timeout {task.test_timeout_s}s; "
            f"extra_body: {extra_body}"
        )
    else:
        max_output_tokens = _output_budget(args)
        extra_body = extra_body_from(args, ScriptedModel.name)
        print(OFFLINE_BANNER)
        provider_info = None
    print(f"sandbox: {report.backend}; checks: {report.summary()}")
    print(f"coding task {task.task_id}: {task.notes}")
    sandbox = Sandbox(report)

    for arm, session_id in zip(arms, session_ids, strict=True):
        config = LoopConfig(
            arm=arm,
            max_calls=max_calls,
            max_provider_attempts=args.max_provider_attempts,
            max_output_tokens=max_output_tokens,
            max_context_chars=args.max_context_chars,
            clear_threshold_chars=args.clear_threshold if arm == "chronological" else None,
            extra_body=extra_body,
        )
        if args.live:
            arm_model = model
        else:
            script = demo_script_for(task) if arm == "method" else retained_script_for(task)
            arm_model = ScriptedModel(script)
        summary = run_coding_task(
            task,
            arm_model,
            config,
            runs_dir=args.runs_dir,
            session_id=session_id,
            provider_info=provider_info,
            sandbox=sandbox,
        )
        _print_coding_summary(arm, summary)
    return 0


def _print_coding_summary(arm: str, summary: dict) -> None:
    grading = summary["grading"]
    print(
        f"[{arm}] termination={summary['termination']} correct={summary['correct']} "
        f"steps={summary['steps']} requests={summary['requests']} "
        f"request_chars min/max/total={summary['request_chars']['min']}/"
        f"{summary['request_chars']['max']}/{summary['request_chars']['total']} "
        f"transitions={summary['context_transitions']} recoveries={summary['recoveries']}"
    )
    if summary["error"]:
        print(f"[{arm}] error: {summary['error']}")
    print(f"[{arm}] answer: {summary['answer']!r}")
    print(
        f"[{arm}] grader: {grading['passed']}/{grading['cases']} cases passed; "
        f"run completed={grading['run']['completed']}; reason: {grading['reason']}"
    )
    _print_costs(summary)
    if summary["usage"]:
        print(f"[{arm}] provider usage per response: {summary['usage']}")
    print(f"[{arm}] session ID: {summary['session_id']}  (files in {summary['run_dir']})")


def _smoke(args: argparse.Namespace, session: str) -> int:
    _refuse_existing(args.runs_dir, [session])
    max_calls = len(SMOKE_PLAN) if args.max_calls is None else args.max_calls
    if max_calls < len(SMOKE_PLAN):
        raise UsageError(
            f"--max-calls {max_calls} is below the {len(SMOKE_PLAN)} prescribed smoke steps"
        )
    if args.live:
        settings = load_settings(args.env)
        extra_body = extra_body_from(args, settings.model)
        max_output_tokens = _output_budget(args, settings.model)
        model = _live_model(args, settings)
        mode = "live"
        provider_info = model.describe()
        print(
            f"LIVE contract smoke against {provider_info['base_url']} model {model.name}; "
            f"caps: {max_calls} calls, {max_output_tokens} output tokens, "
            f"timeout {args.timeout}s; extra_body: {extra_body}"
        )
    else:
        max_output_tokens = _output_budget(args)
        extra_body = extra_body_from(args, ScriptedModel.name)
        model = scripted_follower()
        mode = FIXTURE_MODE
        provider_info = None
        print(OFFLINE_BANNER)
    print(
        "Smoke steps are PRESCRIBED via tool_choice; arguments are chosen by the model. "
        "A passed verdict shows provider acceptance of the edited history, not model judgement."
    )
    report = run_smoke(
        model,
        args.runs_dir,
        session,
        mode=mode,
        max_calls=max_calls,
        max_provider_attempts=args.max_provider_attempts,
        max_output_tokens=max_output_tokens,
        extra_body=extra_body,
        provider_info=provider_info,
    )
    print(f"termination: {report['termination']}")
    for check in report["checks"]:
        print(f"  {check['status']:12s} {check['check']}: {check['detail']}")
    print(f"verdict: {report['verdict']}")
    if mode == FIXTURE_MODE:
        print("fixture self-test: harness checks above; provider acceptance cannot be claimed")
    print(f"session ID: {session}  (files in {Path(args.runs_dir) / session})")
    return VERDICT_EXIT_CODES[report["verdict"]]


def _print_summary(arm: str, summary: dict) -> None:
    print(
        f"[{arm}] termination={summary['termination']} correct={summary['correct']} "
        f"steps={summary['steps']} requests={summary['requests']} "
        f"request_chars min/max/total={summary['request_chars']['min']}/"
        f"{summary['request_chars']['max']}/{summary['request_chars']['total']} "
        f"transitions={summary['context_transitions']} recoveries={summary['recoveries']}"
    )
    if summary["error"]:
        print(f"[{arm}] error: {summary['error']}")
    print(f"[{arm}] answer: {summary['answer']!r} (gold: {summary['gold_answer']!r})")
    _print_costs(summary)
    if summary["usage"]:
        print(f"[{arm}] provider usage per response: {summary['usage']}")
    print(f"[{arm}] session ID: {summary['session_id']}  (files in {summary['run_dir']})")


def _print_costs(summary: dict) -> None:
    costs = summary.get("costs", {})
    if costs.get("status") == "estimated":
        print(
            f"estimated known-usage cost USD "
            f"{costs['known_usage_lower_usd']:.6f}..{costs['known_usage_upper_usd']:.6f}; "
            f"unmetered attempts={costs['unmetered_attempts']}; not an invoice"
        )
    elif costs.get("status") == "unpriced":
        print("cost estimate unavailable: no matching verified tariff; usage retained")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
