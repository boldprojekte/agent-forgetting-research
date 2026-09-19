"""Independently build and grade the OTel currency graceful-shutdown follow-up."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from scripts.otel_noise_runtime import EXPERIMENT
from scripts.otel_noise_tools import OPENTELEMETRY_CPP_VERSION

IMAGE = "agent-forgetting/otel-currency:checkout-noise-01-grader"
CONTAINER = "otel-currency-shutdown-grader"


def command(arguments: list[str], *, cwd: Path, timeout: int = 1200) -> subprocess.CompletedProcess:
    return subprocess.run(
        arguments,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def inspect_value(template: str, *, cwd: Path) -> str:
    outcome = command(
        ["docker", "inspect", "--format", template, CONTAINER], cwd=cwd, timeout=20
    )
    return outcome.stdout.strip() if outcome.returncode == 0 else ""


def wait_ready(*, cwd: Path, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        outcome = command(
            ["docker", "exec", CONTAINER, "sh", "-c", "nc -z 127.0.0.1 7001"],
            cwd=cwd,
            timeout=10,
        )
        if outcome.returncode == 0:
            return True
        if inspect_value("{{.State.Running}}", cwd=cwd) != "true":
            return False
        time.sleep(0.5)
    return False


def start_container(*, cwd: Path) -> tuple[bool, str]:
    command(["docker", "rm", "--force", CONTAINER], cwd=cwd, timeout=20)
    started = command(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            CONTAINER,
            "--env",
            "CURRENCY_PORT=7001",
            "--env",
            "IPV6_ENABLED=false",
            "--env",
            "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317",
            "--env",
            "OTEL_SERVICE_NAME=currency-grader",
            "--env",
            "VERSION=grader",
            IMAGE,
        ],
        cwd=cwd,
        timeout=30,
    )
    if started.returncode != 0:
        return False, started.stdout
    return wait_ready(cwd=cwd), started.stdout


def signal_probe(signal: str, *, cwd: Path) -> dict[str, object]:
    ready, startup = start_container(cwd=cwd)
    pid_one = ""
    elapsed = 0.0
    signal_result = "not attempted"
    if ready:
        pid = command(
            ["docker", "exec", CONTAINER, "cat", "/proc/1/comm"], cwd=cwd, timeout=20
        )
        pid_one = pid.stdout.strip()
        started = time.monotonic()
        if signal == "SIGTERM":
            sent = command(
                ["docker", "stop", "--timeout", "12", CONTAINER], cwd=cwd, timeout=20
            )
            signal_result = sent.stdout.strip()
        else:
            sent = command(
                ["docker", "kill", "--signal", signal, CONTAINER], cwd=cwd, timeout=20
            )
            signal_result = sent.stdout.strip()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if inspect_value("{{.State.Running}}", cwd=cwd) == "false":
                    break
                time.sleep(0.1)
        elapsed = time.monotonic() - started

    running = inspect_value("{{.State.Running}}", cwd=cwd)
    if running == "true":
        command(["docker", "kill", CONTAINER], cwd=cwd, timeout=20)
    exit_text = inspect_value("{{.State.ExitCode}}", cwd=cwd)
    logs = command(["docker", "logs", CONTAINER], cwd=cwd, timeout=20).stdout
    command(["docker", "rm", "--force", CONTAINER], cwd=cwd, timeout=20)
    exit_code = int(exit_text) if exit_text.lstrip("-").isdigit() else None
    passed = (
        ready
        and pid_one == "currency"
        and running == "false"
        and exit_code == 0
        and elapsed < 12
    )
    return {
        "signal": signal,
        "ready": ready,
        "pid_one": pid_one,
        "elapsed_seconds": elapsed,
        "exit_code": exit_code,
        "passed": passed,
        "startup_output": startup,
        "signal_output": signal_result,
        "logs": logs,
    }


def startup_failure_probe(*, cwd: Path) -> dict[str, object]:
    ready, startup = start_container(cwd=cwd)
    if ready:
        listener = command(
            [
                "docker",
                "exec",
                "--detach",
                CONTAINER,
                "sh",
                "-c",
                "nc -l -p 7002 >/dev/null",
            ],
            cwd=cwd,
            timeout=20,
        )
        time.sleep(0.2)
        try:
            conflict = command(
                ["docker", "exec", CONTAINER, "/usr/local/bin/currency", "7002"],
                cwd=cwd,
                timeout=30,
            )
            exit_code = conflict.returncode
            output = conflict.stdout
        except subprocess.TimeoutExpired as error:
            exit_code = None
            output = f"conflicting startup timed out: {error}"
        if listener.returncode != 0:
            output = f"listener failed: {listener.stdout}\n{output}"
    else:
        exit_code = None
        output = "startup failed before the conflict probe"
    command(["docker", "rm", "--force", CONTAINER], cwd=cwd, timeout=20)
    return {
        "ready": ready,
        "exit_code": exit_code,
        "passed": ready and exit_code is not None and 0 < exit_code < 128,
        "startup_output": startup,
        "probe_output": output,
    }


def source_audit(candidate: Path) -> dict[str, bool]:
    source = (candidate / "src/currency/src/server.cpp").read_text()
    tracer = (candidate / "src/currency/src/tracer_common.h").read_text()
    meter = (candidate / "src/currency/src/meter_common.h").read_text()
    logger = (candidate / "src/currency/src/logger_common.h").read_text()
    dockerfile = (candidate / "src/currency/Dockerfile").read_text()
    report_path = candidate / "BENCHMARK_REPORT.md"
    report = report_path.read_text() if report_path.is_file() else ""
    providers = "\n".join((source, tracer, meter, logger))
    direct_or_retained_tracer = bool(
        re.search(r"shared_ptr<[^>]*sdk::trace::TracerProvider>", tracer)
    ) or (
        "tracer_provider" in tracer
        and "static_pointer_cast<opentelemetry::sdk::trace::TracerProvider>" in tracer
    )
    direct_or_retained_meter = "shared_ptr<metric_sdk::MeterProvider>" in meter or (
        "meter_provider" in meter and "static_pointer_cast<metric_sdk::MeterProvider>" in meter
    )
    direct_or_retained_logger = "shared_ptr<logs_sdk::LoggerProvider>" in logger or (
        "logger_provider" in logger and "static_pointer_cast<logs_sdk::LoggerProvider>" in logger
    )
    server_shutdown = source.find("server->Shutdown")
    deadline_creation = source.find("steady_clock::now()")
    return {
        "signal_waiting": all(
            token in source for token in ("pthread_sigmask", "sigwait", "SIGTERM", "SIGINT")
        ),
        "bounded_server_shutdown": bool(
            re.search(r"server->Shutdown\s*\([^;]+(?:deadline|timeout|system_clock)", source)
        ),
        "sdk_providers_retained": (
            direct_or_retained_tracer
            and direct_or_retained_meter
            and direct_or_retained_logger
        ),
        "telemetry_shutdown": "ForceFlush" in providers and providers.count("->Shutdown(") >= 3,
        "shared_remaining_budget": (
            "remaining" in source.lower()
            and deadline_creation >= 0
            and server_shutdown >= 0
            and deadline_creation < server_shutdown
        ),
        "exec_entrypoint": bool(re.search(r"ENTRYPOINT\s*\[[^\n]*\bexec\b", dockerfile)),
        "report": "currency" in report.lower() and "shutdown" in report.lower(),
    }


def grade(candidate: Path) -> tuple[dict[str, object], str]:
    if not (candidate / "src/currency").is_dir():
        raise ValueError(f"candidate is not an OpenTelemetry Demo workspace: {candidate}")

    build = command(
        [
            "docker",
            "build",
            "--build-arg",
            f"OPENTELEMETRY_CPP_VERSION={OPENTELEMETRY_CPP_VERSION}",
            "--file",
            str(candidate / "src/currency/Dockerfile"),
            "--tag",
            IMAGE,
            str(candidate),
        ],
        cwd=EXPERIMENT,
    )
    audit = source_audit(candidate)
    probes: list[dict[str, object]] = []
    startup_probe: dict[str, object] = {}
    if build.returncode == 0:
        startup_probe = startup_failure_probe(cwd=EXPERIMENT)
        probes = [signal_probe("SIGTERM", cwd=EXPERIMENT), signal_probe("SIGINT", cwd=EXPERIMENT)]

    both_signals = len(probes) == 2 and all(bool(probe["passed"]) for probe in probes)
    clean_runtime = build.returncode == 0 and both_signals
    criteria = {
        "startup-failure": build.returncode == 0 and bool(startup_probe.get("passed")),
        "signals": clean_runtime and audit["signal_waiting"],
        "bounded-server": clean_runtime and audit["bounded_server_shutdown"],
        "telemetry": clean_runtime
        and audit["sdk_providers_retained"]
        and audit["telemetry_shutdown"]
        and audit["shared_remaining_budget"],
        "entrypoint": clean_runtime and audit["exec_entrypoint"],
        "followup-report": audit["report"],
    }
    result = {
        "graded_at": datetime.now(UTC).isoformat(),
        "candidate": str(candidate.resolve()),
        "build_exit_code": build.returncode,
        "source_audit": audit,
        "startup_failure_probe": startup_probe,
        "signal_probes": [{k: v for k, v in probe.items() if k != "logs"} for probe in probes],
        "criteria": criteria,
        "criteria_passed": sum(criteria.values()),
        "criteria_total": len(criteria),
        "passed": all(criteria.values()),
    }
    probe_output = "\n".join(
        f"{probe['signal']} logs:\n{probe['logs']}" for probe in probes
    )
    output = f"Currency image build:\n{build.stdout}\n{probe_output}"
    return result, output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result, output = grade(args.candidate.resolve())
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(result, indent=2) + "\n")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
