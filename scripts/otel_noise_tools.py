"""Bounded model-facing deployment and test tools for the OTel noise benchmark."""

from __future__ import annotations

import fnmatch
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from forgetting_agent.tools import ToolResult
from scripts.otel_noise_runtime import ORIGIN, RUNTIME, compose_command

OUTPUT_LIMIT = 20_000
FRONTEND_IMAGE = "agent-forgetting/otel-frontend:checkout-noise-01"
CYPRESS_IMAGE = "agent-forgetting/otel-cypress:checkout-noise-01"
CURRENCY_IMAGE = "agent-forgetting/otel-currency:checkout-noise-01"
OPENTELEMETRY_CPP_VERSION = "1.27.0"

FRONTEND_EXISTING = {
    "src/frontend/pages/api/checkout.ts",
    "src/frontend/utils/Request.ts",
}
FRONTEND_NEW = {
    "BENCHMARK_REPORT.md",
    "src/frontend/cypress/e2e/*.cy.ts",
}
CURRENCY_EXISTING = {
    "src/currency/CMakeLists.txt",
    "src/currency/Dockerfile",
    "src/currency/src/server.cpp",
    "src/currency/src/logger_common.h",
    "src/currency/src/meter_common.h",
    "src/currency/src/tracer_common.h",
    "BENCHMARK_REPORT.md",
}

CUSTOM_SCHEMAS = {
    "redeploy_frontend": {
        "description": (
            "Build the current workspace frontend with the benchmark's trusted Dockerfile, "
            "replace the live frontend container, and wait for health. Use after code changes "
            "before browser verification. The exact build is fixed by the harness; no command "
            "or image arguments are accepted. A failed build changes no live container."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "run_frontend_tests": {
        "description": (
            "Build the current workspace's Cypress test image with the benchmark's trusted "
            "Dockerfile and run selected frontend Cypress specs against the live stack. Use a "
            "focused new regression spec first. Paths are validated under "
            "src/frontend/cypress/e2e; no shell or options are accepted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "specs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {"type": "string"},
                    "description": (
                        "One to five existing paths matching src/frontend/cypress/e2e/*.cy.ts."
                    ),
                }
            },
            "required": ["specs"],
            "additionalProperties": False,
        },
    },
    "redeploy_currency": {
        "description": (
            "Build the current workspace currency service with its workspace Dockerfile and the "
            "benchmark-pinned OpenTelemetry C++ version, replace only the live currency container, "
            "and wait for health. Use after currency changes. The fixed build accepts no command "
            "or image arguments."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "probe_currency_shutdown": {
        "description": (
            "Send SIGTERM to the live candidate currency container, observe bounded exit time "
            "and its final logs, then restart it. Call after redeploy_currency to verify signal "
            "forwarding and telemetry shutdown behavior. The probe is fixed and accepts no "
            "signal, command, timeout or container arguments."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _bounded_output(result: subprocess.CompletedProcess[str]) -> str:
    output = result.stdout or ""
    if len(output) > OUTPUT_LIMIT:
        output = output[-OUTPUT_LIMIT:]
        output = f"[earlier output omitted; final {OUTPUT_LIMIT} chars shown]\n{output}"
    return output.rstrip()


def _run(command: list[str], *, cwd: Path, timeout: float) -> ToolResult:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        partial = (error.stdout or "")[-OUTPUT_LIMIT:]
        return ToolResult(
            f"Operation timed out after {timeout:.0f}s; completion state is unknown. "
            f"Inspect live state before retrying.\n{partial}",
            ok=False,
        )
    except OSError as error:
        return ToolResult(f"Operation could not start: {type(error).__name__}: {error}", ok=False)
    elapsed = time.monotonic() - started
    output = _bounded_output(result)
    return ToolResult(
        f"exit code {result.returncode}; elapsed {elapsed:.1f}s"
        + (f"\n{output}" if output else ""),
        ok=result.returncode == 0,
    )


class OtelNoiseTools:
    """Add fixed Docker actions and constrain edits/browser navigation by task phase."""

    browser_allowed = {
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_find",
        "browser_click",
        "browser_press_key",
        "browser_take_screenshot",
        "browser_wait_for",
        "browser_type",
        "browser_fill_form",
        "browser_select_option",
        "browser_handle_dialog",
        "browser_tabs",
        "browser_resize",
        "browser_console_messages",
        "browser_network_requests",
        "browser_hover",
        "browser_drag",
    }

    def __init__(self, base, workspace, phase: str = "primary") -> None:
        self.base = base
        self.workspace = workspace
        self.phase = phase

    @property
    def names(self) -> tuple[str, ...]:
        names = tuple(
            name
            for name in self.base.names
            if name != "run_tests"
            and (
                not name.startswith("mcp_browser__")
                or name.removeprefix("mcp_browser__") in self.browser_allowed
            )
        )
        custom = (
            ("redeploy_frontend", "run_frontend_tests")
            if self.phase == "primary"
            else ("redeploy_currency", "probe_currency_shutdown")
        )
        return (*names, *custom)

    @property
    def schemas(self):
        return {
            **{name: self.base.schemas[name] for name in self.names if name in self.base.schemas},
            **{name: CUSTOM_SCHEMAS[name] for name in self.names if name in CUSTOM_SCHEMAS},
        }

    def _can_change(self, name: str, arguments: dict) -> ToolResult | None:
        if name not in {"edit_file", "write_file", "remove_file"}:
            return None
        path = str(arguments.get("path", ""))
        if self.phase == "primary":
            allowed = path in FRONTEND_EXISTING or any(
                fnmatch.fnmatchcase(path, pattern) for pattern in FRONTEND_NEW
            )
        else:
            allowed = path in CURRENCY_EXISTING
        if allowed:
            return None
        return ToolResult(
            f"This benchmark phase does not allow changing {path!r}. Keep the fix scoped to "
            "the task files described in the request; Docker, environment, feature flags and "
            "unrelated services are protected.",
            ok=False,
        )

    def call(self, name: str, arguments: dict) -> ToolResult:
        denied = self._can_change(name, arguments)
        if denied is not None:
            return denied
        if name == "mcp_browser__browser_navigate":
            url = urlsplit(str(arguments.get("url", "")))
            if f"{url.scheme}://{url.netloc}" != ORIGIN:
                return ToolResult(
                    f"Browser navigation is limited to the benchmark origin {ORIGIN}. "
                    "All shop and observability routes are available below it.",
                    ok=False,
                )
        if name == "redeploy_frontend":
            return self._redeploy_frontend()
        if name == "run_frontend_tests":
            return self._run_frontend_tests(arguments.get("specs"))
        if name == "redeploy_currency":
            return self._redeploy_currency()
        if name == "probe_currency_shutdown":
            return self._probe_currency_shutdown()
        return self.base.call(name, arguments)

    def _redeploy_frontend(self) -> ToolResult:
        result = _run(
            [
                "docker",
                "build",
                "--file",
                str(RUNTIME / "src/frontend/Dockerfile"),
                "--tag",
                FRONTEND_IMAGE,
                str(self.workspace.root),
            ],
            cwd=RUNTIME,
            timeout=900,
        )
        if not result.ok:
            return result
        deploy = _run(
            compose_command(
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "120",
                "--no-deps",
                "--force-recreate",
                "frontend",
            ),
            cwd=RUNTIME,
            timeout=180,
        )
        return ToolResult(
            f"Frontend build:\n{result.text}\nFrontend deploy:\n{deploy.text}", deploy.ok
        )

    def _run_frontend_tests(self, specs) -> ToolResult:
        if not isinstance(specs, list) or not 1 <= len(specs) <= 5:
            return ToolResult("specs must contain one to five Cypress spec paths.", ok=False)
        checked = []
        for item in specs:
            if not isinstance(item, str) or not fnmatch.fnmatchcase(
                item, "src/frontend/cypress/e2e/*.cy.ts"
            ):
                return ToolResult(
                    f"Invalid spec {item!r}; use src/frontend/cypress/e2e/*.cy.ts.", ok=False
                )
            if not (self.workspace.root / item).is_file():
                return ToolResult(f"Spec {item!r} does not exist in the workspace.", ok=False)
            checked.append(item.removeprefix("src/frontend/"))
        build = _run(
            [
                "docker",
                "build",
                "--file",
                str(RUNTIME / "src/frontend/Dockerfile.cypress"),
                "--tag",
                CYPRESS_IMAGE,
                str(self.workspace.root),
            ],
            cwd=RUNTIME,
            timeout=900,
        )
        if not build.ok:
            return build
        test = _run(
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
                ",".join(checked),
            ],
            cwd=RUNTIME,
            timeout=900,
        )
        return ToolResult(f"Cypress image build:\n{build.text}\nCypress run:\n{test.text}", test.ok)

    def _redeploy_currency(self) -> ToolResult:
        build = _run(
            [
                "docker",
                "build",
                "--build-arg",
                f"OPENTELEMETRY_CPP_VERSION={OPENTELEMETRY_CPP_VERSION}",
                "--file",
                str(self.workspace.root / "src/currency/Dockerfile"),
                "--tag",
                CURRENCY_IMAGE,
                str(self.workspace.root),
            ],
            cwd=RUNTIME,
            timeout=1200,
        )
        if not build.ok:
            return build
        command = compose_command()
        command.extend(("--file", str(RUNTIME / "compose.followup.yaml")))
        command.extend(
            (
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "120",
                "--no-deps",
                "--force-recreate",
                "currency",
            )
        )
        deploy = _run(command, cwd=RUNTIME, timeout=180)
        return ToolResult(
            f"Currency build:\n{build.text}\nCurrency deploy:\n{deploy.text}", deploy.ok
        )

    def _probe_currency_shutdown(self) -> ToolResult:
        started = time.monotonic()
        stop = _run(
            ["docker", "stop", "--timeout", "12", "currency"], cwd=RUNTIME, timeout=20
        )
        elapsed = time.monotonic() - started
        inspect = _run(
            ["docker", "inspect", "--format", "{{.State.ExitCode}}", "currency"],
            cwd=RUNTIME,
            timeout=20,
        )
        logs = _run(["docker", "logs", "--tail", "120", "currency"], cwd=RUNTIME, timeout=20)
        command = compose_command()
        command.extend(("--file", str(RUNTIME / "compose.followup.yaml")))
        command.extend(
            (
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "120",
                "--no-deps",
                "currency",
            )
        )
        restart = _run(command, cwd=RUNTIME, timeout=150)
        clean_exit = inspect.ok and inspect.text.rstrip().endswith("\n0")
        bounded = elapsed < 12.5
        return ToolResult(
            f"SIGTERM stop elapsed {elapsed:.1f}s\nStop:\n{stop.text}\n"
            f"Exit status:\n{inspect.text}\nFinal logs:\n{logs.text}\nRestart:\n{restart.text}",
            ok=stop.ok and clean_exit and bounded and logs.ok and restart.ok,
        )
