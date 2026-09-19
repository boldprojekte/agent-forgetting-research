"""Run one controlled OTel checkout-noise arm, optionally with its same-session pivot."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path

from forgetting_agent.costs import fetch_pricing
from forgetting_agent.experiment import summarise_run, write_run_files
from forgetting_agent.loop import (
    LoopConfig,
    SubmissionCriterion,
    build_system_prompt,
    run_episode,
)
from forgetting_agent.mcp_tools import open_mcp_tools
from forgetting_agent.provider import (
    GLM_53_MAX_OUTPUT_TOKENS,
    OpenAIModel,
    load_settings,
    thinking_extra_body,
)
from forgetting_agent.trace import Trace
from forgetting_agent.workspace import Workspace
from integrations.playwright.client import browser_client
from scripts.otel_noise_runtime import (
    ORIGIN,
    build_runtime_images,
    reset_flags,
    wait_for_origin,
    wait_for_stack,
)
from scripts.otel_noise_runtime import run as compose_run
from scripts.otel_noise_tools import OtelNoiseTools

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments" / "otel-checkout-noise-03"
BASE_EXPERIMENT = ROOT / "experiments" / "otel-checkout-noise-01"
SOURCE = BASE_EXPERIMENT / "snapshot"

PRIMARY_CRITERIA = (
    SubmissionCriterion(
        "diagnosis",
        "Diagnose the checkout failure from the live application and correlated "
        "observability evidence before editing.",
        "Name the failing request and services plus concrete trace, log or metric "
        "evidence in BENCHMARK_REPORT.md.",
    ),
    SubmissionCriterion(
        "payment-contract",
        "Expected simulated payment declines return HTTP 422 with a safe non-empty "
        "error and code PAYMENT_FAILED.",
        "A focused live Cypress assertion passes against the redeployed candidate.",
    ),
    SubmissionCriterion(
        "internal-contract",
        "Unrelated internal checkout failures remain generic HTTP 500 JSON responses with a safe "
        "non-empty error and without PAYMENT_FAILED or leaked upstream details.",
        "A separate focused live assertion passes for a non-payment internal failure.",
    ),
    SubmissionCriterion(
        "request-helper",
        "The shared request helper rejects non-success responses, preserves JSON server "
        "messages, and safely handles non-JSON error bodies.",
        "Code inspection and focused test evidence cover JSON and non-JSON error bodies.",
    ),
    SubmissionCriterion(
        "verification",
        "Successful behavior and error telemetry are preserved, with changes and actual "
        "outcomes documented.",
        "Redeployment succeeds, focused regression tests pass, browser or API behavior is "
        "observed, and BENCHMARK_REPORT.md is complete.",
    ),
)

FOLLOWUP_CRITERIA = (
    SubmissionCriterion(
        "startup-failure",
        "Currency server startup failures exit nonzero without waiting for a termination signal.",
        "A focused occupied-port probe or equivalent test demonstrates a prompt nonzero exit.",
    ),
    SubmissionCriterion(
        "signals",
        "The currency service handles SIGTERM and SIGINT without "
        "asynchronous-signal-unsafe shutdown work.",
        "Source and build evidence show a race-safe signal-waiting design.",
    ),
    SubmissionCriterion(
        "bounded-server",
        "Shutdown stops accepting work and gives the gRPC server a bounded opportunity to finish.",
        "Source identifies the bounded server deadline and the live shutdown probe completes.",
    ),
    SubmissionCriterion(
        "telemetry",
        "Tracing, metrics and logging providers flush and shut down within one bounded "
        "overall shutdown deadline that starts when the termination signal is received and "
        "includes the server drain.",
        "Source and final logs show ordered provider flush and shutdown under the shared "
        "remaining deadline.",
    ),
    SubmissionCriterion(
        "entrypoint",
        "The container command forwards termination signals to the currency process.",
        "Dockerfile inspection confirms exec-form signal forwarding and the candidate "
        "image builds.",
    ),
    SubmissionCriterion(
        "followup-report",
        "The follow-up implementation and verification are recorded without overwriting "
        "the primary findings.",
        "BENCHMARK_REPORT.md contains distinct primary and currency sections with actual "
        "outcomes and limitations.",
    ),
)


def encode(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(type(value).__name__)


def state_json(state) -> str:
    return json.dumps(vars(state), default=encode, ensure_ascii=False, sort_keys=True)


def reset_stack() -> None:
    compose_run("down", "--volumes", "--remove-orphans", check=False)
    reset_flags()
    build_runtime_images()
    compose_run("up", "--detach", "--no-build")
    wait_for_stack()
    wait_for_origin()


def config(arm: str, criteria: tuple[SubmissionCriterion, ...]) -> LoopConfig:
    return LoopConfig(
        arm=arm,
        max_calls=1500,
        max_provider_attempts=1800,
        max_output_tokens=GLM_53_MAX_OUTPUT_TOKENS,
        extra_body={**thinking_extra_body("z-ai/glm-5.3-flash", True), "reasoning_effort": "low"},
        required_submission_criteria=criteria,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True, choices=("method", "retained"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stages", choices=("primary", "both"), default="both")
    args = parser.parse_args()

    run_root = ROOT / "runs" / args.run_id
    run_root.mkdir(exist_ok=False)
    (run_root / "browser").mkdir()
    (run_root / "primary").mkdir()
    if args.stages == "both":
        (run_root / "followup").mkdir()
    (run_root / "task.md").write_text((EXPERIMENT / "task.md").read_text())
    (run_root / "followup-task.md").write_text((EXPERIMENT / "followup-task.md").read_text())
    reset_stack()

    workspace = Workspace.from_snapshot(SOURCE, run_root)
    settings = replace(load_settings(ROOT / ".env"), model="z-ai/glm-5.3-flash")
    model = OpenAIModel(
        settings.api_key,
        settings.base_url,
        settings.model,
        supports_images=True,
        timeout=360,
        max_attempts=6,
        backoff_cap=60,
    )
    trace = Trace(run_root / "trace.jsonl")
    try:
        model.pricing = fetch_pricing(settings.base_url, settings.api_key, settings.model)
        primary_config = config(args.arm, PRIMARY_CRITERIA)
        trace.event(
            "run_start",
            session_id=args.run_id,
            task_id="otel-checkout-noise-03-primary",
            model=model.name,
            config=asdict(primary_config),
            provider=model.describe(),
            origin=ORIGIN,
            stages=args.stages,
        )
        with open_mcp_tools(
            workspace,
            {"browser": browser_client(run_root / "browser")},
            trace,
            timeout=120,
        ) as base:
            tools = OtelNoiseTools(base, workspace, "primary")
            trace.event("exposed_tools", phase="primary", names=tools.names, schemas=tools.schemas)
            primary_task = (
                EXPERIMENT / "task.md"
            ).read_text().strip() + f"\n\nThe benchmark origin is {ORIGIN}."
            primary = run_episode(
                primary_task,
                tools,
                model,
                primary_config,
                trace,
                system_prompt=build_system_prompt(args.arm, "coding_shared.md"),
            )
            primary_summary = summarise_run(
                trace,
                primary,
                model,
                primary_config,
                "otel-checkout-noise-03-primary",
                args.run_id,
                run_root,
            )
            write_run_files(run_root / "primary", primary_summary, primary.state)
            primary_state = state_json(primary.state)
            (run_root / "primary/context-state.json").write_text(primary_state)
            (run_root / "primary/state.sha256").write_text(
                hashlib.sha256(primary_state.encode()).hexdigest() + "\n"
            )
            if args.stages == "primary" or primary.termination != "submitted":
                result = primary
            else:
                tools.phase = "followup"
                followup_config = config(args.arm, FOLLOWUP_CRITERIA)
                trace.event(
                    "stage_start",
                    phase="followup",
                    task_id="otel-checkout-noise-03-followup",
                    config=asdict(followup_config),
                )
                trace.event(
                    "exposed_tools", phase="followup", names=tools.names, schemas=tools.schemas
                )
                result = run_episode(
                    (EXPERIMENT / "followup-task.md").read_text(),
                    tools,
                    model,
                    followup_config,
                    trace,
                    initial_state=primary.state,
                )
                followup_summary = summarise_run(
                    trace,
                    result,
                    model,
                    followup_config,
                    "otel-checkout-noise-03-followup",
                    args.run_id,
                    run_root,
                )
                write_run_files(run_root / "followup", followup_summary, result.state)
        print(
            json.dumps(
                {
                    "run": args.run_id,
                    "arm": args.arm,
                    "primary_termination": primary.termination,
                    "primary_steps": primary.steps,
                    "final_termination": result.termination,
                    "final_steps": result.steps,
                }
            ),
            flush=True,
        )
    finally:
        trace.close()
        model._client.close()


if __name__ == "__main__":
    main()
