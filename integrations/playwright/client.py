"""Project-local, pinned Playwright MCP connection for controlled browser experiments."""

import shutil
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent


def browser_client(output_dir: Path) -> Client:
    # Obtain the matching binary via the installed Playwright package, not a guessed revision.
    import os
    import subprocess

    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / "browsers")}
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required for Playwright MCP")
    executable = subprocess.check_output(
        [node, "-e", "console.log(require('playwright').chromium.executablePath())"],
        cwd=ROOT,
        env=env,
        text=True,
    ).strip()
    return Client(
        StdioServerParameters(
            command=node,
            args=[
                str(ROOT / "node_modules/@playwright/mcp/cli.js"),
                "--config",
                str(ROOT / "config.json"),
                "--headless",
                "--isolated",
                "--executable-path",
                executable,
                "--caps",
                "vision",
                "--image-responses",
                "allow",
                "--output-dir",
                str(output_dir.resolve()),
            ],
            env={"PLAYWRIGHT_BROWSERS_PATH": str(ROOT / "browsers")},
            cwd=str(output_dir.resolve()),
        ),
        read_timeout_seconds=60,
        cache=None,
    )
