"""Task-specific observation worker. Invoked ONLY inside Sandbox via trusted -c text.

No expected values here. Fixtures and command entrypoints are harness-defined, never
model-supplied bash. Each case gets a fresh /tmp HOME and repository copy with spaces.
"""

import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

BOUNDARIES = [
    "omarchy-theme-bg-next",
    "pgrep",
    "omarchy-restart-waybar",
    "omarchy-restart-swayosd",
    "omarchy-restart-terminal",
    "omarchy-restart-hyprctl",
    "omarchy-restart-btop",
    "omarchy-restart-opencode",
    "omarchy-restart-mako",
    "omarchy-theme-set-gnome",
    "omarchy-theme-set-browser",
    "omarchy-theme-set-vscode",
    "omarchy-theme-set-obsidian",
    "omarchy-theme-set-keyboard",
    "omarchy-hook",
]


def fingerprint(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def observe(case):
    if Path.cwd() != Path("/work") or Path.home() != Path("/tmp"):
        raise RuntimeError("theme runner requires sandbox /work and disposable HOME")
    with tempfile.TemporaryDirectory(prefix="legacy theme ") as tmp:
        base = Path(tmp)
        repo = base / "repo with spaces"
        shutil.copytree("/work", repo)
        home = base / "home with spaces"
        home.mkdir()
        stock = repo / "themes/pilot"
        user = home / ".config/omarchy/themes/pilot"
        for where, files in ((stock, case.get("stock", {})), (user, case.get("user", {}))):
            where.mkdir(parents=True)
            for name, text in files.items():
                (where / name).write_text(text)
        templates = home / ".config/omarchy/themed"
        templates.mkdir(parents=True)
        for name, text in case.get("templates", {}).items():
            (templates / name).write_text(text)
        before = {"stock": fingerprint(stock), "user": fingerprint(user)}
        stubs = base / "safe commands"
        stubs.mkdir()
        log = base / "boundaries.log"
        # Workspace write_file creates 0644 files. Normalize only this disposable copy;
        # honour helpers' own shebangs rather than imposing Bash or a helper filename.
        for path in (repo / "bin").iterdir():
            if path.is_file():
                path.chmod(path.stat().st_mode | 0o111)
        for name in BOUNDARIES:
            stub = stubs / name
            stub.write_text(
                "#!/bin/bash\nprintf '%s\\n' " + shlex.quote(name) + ' >> "$BOUNDARY_LOG"\n'
            )
            stub.chmod(0o755)
        env = {
            "HOME": str(home),
            "OMARCHY_PATH": str(repo),
            "LANG": "C.UTF-8",
            "PATH": str(stubs) + ":" + str(repo / "bin") + ":/usr/bin:/bin",
            "BOUNDARY_LOG": str(log),
        }
        run = subprocess.run(
            ["bash", str(repo / "bin/omarchy-theme-set"), "Pilot"],
            env=env,
            capture_output=True,
            timeout=12,
        )
        current = home / ".config/omarchy/current/theme"
        colors = current / "colors.toml"
        try:
            palette = tomllib.loads(colors.read_text()) if colors.exists() else None
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            palette = {"invalid_generated_toml": True}
        template_names = {
            p.name.removesuffix(".tpl")
            for folder in (repo / "default/themed", templates)
            for p in folder.glob("*.tpl")
        }
        template_names -= set(before["stock"]) | set(before["user"])
        return {
            "template_files": sorted(name for name in template_names if (current / name).exists()),
            "exit": run.returncode,
            "palette": palette,
            "ghostty": (current / "ghostty.conf").read_text()
            if (current / "ghostty.conf").exists()
            else None,
            "colors_text": colors.read_text() if case.get("preserve_colors") else None,
            "source_unchanged": before == {"stock": fingerprint(stock), "user": fingerprint(user)},
            "boundaries": log.read_text().splitlines() if log.exists() else [],
            "theme_name": (home / ".config/omarchy/current/theme.name").read_text().strip()
            if (home / ".config/omarchy/current/theme.name").exists()
            else None,
        }


def main():
    job = json.load(sys.stdin)
    for case in job["cases"]:
        try:
            record = {"id": case["id"], "value": observe(case["fixture"])}
        except Exception as error:
            record = {"id": case["id"], "error": f"{type(error).__name__}: {error}"[:500]}
        print(json.dumps(record), flush=True)
    print(json.dumps({"grader_done": len(job["cases"])}), flush=True)


if __name__ == "__main__":
    main()
