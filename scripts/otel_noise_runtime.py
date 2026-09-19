"""Operate the pinned OpenTelemetry Demo stack used by the checkout-noise benchmark."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments" / "otel-checkout-noise-01"
RUNTIME = EXPERIMENT / "private" / "runtime"
ORIGIN = "http://127.0.0.1:18080"
PROJECT = "otel-noise-01"
COMPOSE_FILES = (
    "compose.yaml",
    "compose.full.yaml",
    "compose.observability.yaml",
    "compose.extras.yaml",
    "compose.benchmark.yaml",
)


def reset_flags() -> None:
    """Atomically restore the private fault seed after flagd-ui rewrites its live file."""
    directory = RUNTIME / "src" / "flagd"
    seed = directory / "demo.flagd.benchmark.json"
    live = directory / "demo.flagd.json"
    data = seed.read_bytes()
    fd, temporary = tempfile.mkstemp(prefix=".benchmark-flags-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, live)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def compose_command(*arguments: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "--env-file",
        str(RUNTIME / ".env"),
        "--env-file",
        str(RUNTIME / ".env.benchmark"),
    ]
    for name in COMPOSE_FILES:
        command.extend(("--file", str(RUNTIME / name)))
    return [*command, *arguments]


def run(*arguments: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        compose_command(*arguments),
        cwd=RUNTIME,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def build_frontend(context: Path = RUNTIME) -> None:
    context = context.resolve()
    trusted = (RUNTIME / "src" / "frontend" / "Dockerfile").resolve()
    subprocess.run(
        [
            "docker",
            "build",
            "--file",
            str(trusted),
            "--tag",
            "agent-forgetting/otel-frontend:checkout-noise-01",
            str(context),
        ],
        check=True,
        cwd=RUNTIME,
    )


def build_checkout() -> None:
    """Build the exact checkout dependency from the pinned benchmark base."""
    trusted = (RUNTIME / "src" / "checkout" / "Dockerfile").resolve()
    subprocess.run(
        [
            "docker",
            "build",
            "--file",
            str(trusted),
            "--tag",
            "agent-forgetting/otel-checkout:checkout-noise-01",
            str(RUNTIME),
        ],
        check=True,
        cwd=RUNTIME,
    )


def build_payment() -> None:
    """Build the exact payment dependency from the pinned benchmark base."""
    trusted = (RUNTIME / "src" / "payment" / "Dockerfile").resolve()
    subprocess.run(
        [
            "docker",
            "build",
            "--file",
            str(trusted),
            "--tag",
            "agent-forgetting/otel-payment:checkout-noise-01",
            str(RUNTIME),
        ],
        check=True,
        cwd=RUNTIME,
    )


def build_runtime_images() -> None:
    build_frontend()
    build_checkout()
    build_payment()


def http_json(path: str, *, method: str = "GET", body: Any = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{ORIGIN}{path}",
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        decoded = raw.decode("utf-8", "replace")[:1000]
    return status, decoded


def wait_for_origin(timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{ORIGIN}/", timeout=10) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = str(error)
        time.sleep(3)
    raise RuntimeError(f"frontend did not become ready within {timeout:.0f}s: {last_error}")


def wait_for_stack(timeout: float = 600) -> None:
    """Wait until every declared Compose service is running and healthy when checked."""
    expected = set(run("config", "--services", capture=True).stdout.splitlines())
    deadline = time.monotonic() + timeout
    last_problem = "stack not inspected"
    while time.monotonic() < deadline:
        outcome = run("ps", "--format", "json", check=False, capture=True)
        rows = []
        try:
            rows = [json.loads(line) for line in outcome.stdout.splitlines() if line.strip()]
        except json.JSONDecodeError:
            pass
        by_service = {row.get("Service"): row for row in rows}
        missing = sorted(expected - set(by_service))
        bad = sorted(
            f"{name}={row.get('State')}/{row.get('Health') or 'no-healthcheck'}"
            for name, row in by_service.items()
            if row.get("State") != "running" or (row.get("Health") or "") not in ("", "healthy")
        )
        if not missing and not bad:
            return
        last_problem = f"missing={missing}; not-ready={bad}"
        time.sleep(3)
    raise RuntimeError(f"stack did not become ready within {timeout:.0f}s: {last_problem}")


def smoke() -> dict[str, Any]:
    surfaces: dict[str, int] = {}
    for path in ("/", "/feature/", "/jaeger/ui/", "/grafana/"):
        try:
            with urllib.request.urlopen(f"{ORIGIN}{path}", timeout=30) as response:
                surfaces[path] = response.status
        except urllib.error.HTTPError as error:
            surfaces[path] = error.code

    user_id = f"benchmark-smoke-{int(time.time())}"
    cart_status, _ = http_json(
        "/api/cart",
        method="POST",
        body={"userId": user_id, "item": {"productId": "0PUK6V6EV0", "quantity": 1}},
    )
    checkout_status, checkout_body = http_json(
        "/api/checkout?currencyCode=USD",
        method="POST",
        body={
            "userId": user_id,
            "userCurrency": "USD",
            "address": {
                "streetAddress": "1600 Amphitheatre Parkway",
                "city": "Mountain View",
                "state": "CA",
                "country": "USA",
                "zipCode": "94043",
            },
            "email": "benchmark@example.com",
            "creditCard": {
                "creditCardNumber": "4432801561520454",
                "creditCardCvv": 123,
                "creditCardExpirationYear": 2030,
                "creditCardExpirationMonth": 1,
            },
        },
    )
    return {
        "origin": ORIGIN,
        "surfaces": surfaces,
        "cart_status": cart_status,
        "checkout_status": checkout_status,
        "checkout_body": checkout_body,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "status", "smoke", "stop", "reset"))
    args = parser.parse_args()
    if args.action == "start":
        reset_flags()
        build_runtime_images()
        run("up", "--detach", "--no-build")
        wait_for_stack()
        wait_for_origin()
        print(json.dumps(smoke(), indent=2))
    elif args.action == "status":
        outcome = run("ps", "--format", "json", check=False, capture=True)
        print(outcome.stdout, end="")
        raise SystemExit(outcome.returncode)
    elif args.action == "smoke":
        wait_for_origin(30)
        print(json.dumps(smoke(), indent=2))
    elif args.action == "stop":
        run("down", "--remove-orphans")
    elif args.action == "reset":
        run("down", "--volumes", "--remove-orphans")
        reset_flags()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(f"command failed with exit code {error.returncode}", file=sys.stderr)
        raise SystemExit(error.returncode) from error
