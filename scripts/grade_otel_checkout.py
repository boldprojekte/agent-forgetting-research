"""Grade an OTel checkout benchmark workspace with the private upstream oracle."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.otel_noise_runtime import (
    EXPERIMENT,
    RUNTIME,
    build_checkout,
    build_frontend,
    build_payment,
    reset_flags,
    run,
    wait_for_origin,
    wait_for_stack,
)

CYPRESS_IMAGE = "agent-forgetting/otel-cypress:checkout-noise-01-grader"
ORACLE = EXPERIMENT / "private" / "grader" / "CheckoutPaymentFailure.cy.ts"
SPEC = Path("src/frontend/cypress/e2e/CheckoutPaymentFailure.cy.ts")


def command(arguments: list[str], *, cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        arguments,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def grade(candidate: Path) -> tuple[dict[str, object], str]:
    if not (candidate / "src" / "frontend").is_dir():
        raise ValueError(f"candidate is not an OpenTelemetry Demo workspace: {candidate}")
    if not ORACLE.is_file():
        raise FileNotFoundError(f"private oracle missing: {ORACLE}")

    with tempfile.TemporaryDirectory(prefix="otel-checkout-grade-") as directory:
        workspace = Path(directory) / "candidate"
        shutil.copytree(
            candidate,
            workspace,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "node_modules", ".next", "screenshots", "videos"),
        )
        destination = workspace / SPEC
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ORACLE, destination)

        run("down", "--volumes", "--remove-orphans", check=False)
        reset_flags()
        build_checkout()
        build_payment()
        build_frontend(workspace)
        run("up", "--detach", "--no-build")
        wait_for_stack(900)
        wait_for_origin(120)

        build = command(
            [
                "docker",
                "build",
                "--file",
                str(RUNTIME / "src" / "frontend" / "Dockerfile.cypress"),
                "--tag",
                CYPRESS_IMAGE,
                str(workspace),
            ],
            cwd=RUNTIME,
        )
        if build.returncode == 0:
            test = command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "opentelemetry-demo",
                    "--env",
                    "NODE_ENV=production",
                    "--env",
                    "FRONTEND_ADDR=frontend-proxy:8080",
                    CYPRESS_IMAGE,
                    "--browser",
                    "electron",
                    "--spec",
                    str(SPEC.relative_to("src/frontend")),
                ],
                cwd=RUNTIME,
            )
        else:
            test = build

    result = {
        "graded_at": datetime.now(UTC).isoformat(),
        "candidate": str(candidate.resolve()),
        "oracle": str(ORACLE),
        "build_exit_code": build.returncode,
        "test_exit_code": test.returncode,
        "passed": build.returncode == 0 and test.returncode == 0,
    }
    output = f"Cypress image build:\n{build.stdout}\nHidden Cypress test:\n{test.stdout}"
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
