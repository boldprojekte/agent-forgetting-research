"""Reproducible derived snapshot of a pinned public repository for a coding task.

A coding task whose `task.json` carries a `source` block is a *derived fixture*: a small
subset of files from one pinned commit of a public repository, copied byte for byte (git mode
kept) into `snapshot/`. The canonical clone is only read (`git ls-tree`, `git cat-file`);
its working tree is never touched.

    "source": {
      "repository": "https://github.com/omacom/omarchy.git",
      "commit": "<40 hex>",
      "license": "MIT (LICENSE is part of the snapshot)",
      "files": ["LICENSE", "bin/...", ...],
      "adaptation": null            # or a patch file name next to task.json
    }

`build` writes the files and `manifest.json` (repository, commit, per file: git mode, git
blob id, sha256, size). `verify` checks the committed snapshot against the manifest, and,
when the clone is available, every blob against the pinned commit. An adaptation (a change to
upstream files needed to run the subset) is an explicit patch listed in `source.adaptation`;
`null` means the snapshot is byte-identical to upstream. No adaptation is applied by this
module: when one is listed it must be applied by hand and the manifest records the patched
hashes, so that a mismatch between manifest and snapshot is always detectable.

CLI: `python -m forgetting_agent.omarchy_snapshot --task <name> --clone <path> [--build]`.
Without `--build` it only verifies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from .tree import inspect_tree

MODE_FILE = "100644"
MODE_EXEC = "100755"
COMMIT_LEN = 40


class SnapshotError(RuntimeError):
    pass


def _git(clone: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(clone), *args], capture_output=True, check=False, timeout=60
    )
    if result.returncode != 0:
        raise SnapshotError(
            f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def load_source(task_dir: Path) -> dict:
    spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    source = spec.get("source")
    if not isinstance(source, dict):
        raise SnapshotError(f"{task_dir / 'task.json'} has no 'source' block")
    commit = source.get("commit", "")
    if len(commit) != COMMIT_LEN or any(c not in "0123456789abcdef" for c in commit):
        raise SnapshotError(f"source.commit must be a full lowercase sha1, got {commit!r}")
    files = source.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        raise SnapshotError("source.files must be a non-empty list of repository paths")
    if "LICENSE" not in files:
        raise SnapshotError("source.files must include LICENSE (license accompanies the fixture)")
    return source


def _blob_entries(clone: Path, commit: str, files: list[str]) -> dict[str, tuple[str, str]]:
    """path -> (git mode, blob id) for every requested path at `commit`."""
    listing = _git(clone, "ls-tree", "-z", commit, "--", *files).decode("utf-8")
    entries: dict[str, tuple[str, str]] = {}
    for item in listing.split("\0"):
        if not item:
            continue
        meta, path = item.split("\t", 1)
        mode, kind, blob = meta.split(" ")
        if kind != "blob" or mode not in (MODE_FILE, MODE_EXEC):
            raise SnapshotError(f"{path} is not a regular file blob at {commit} ({mode} {kind})")
        entries[path] = (mode, blob)
    missing = [f for f in files if f not in entries]
    if missing:
        raise SnapshotError(f"not in commit {commit}: {missing}")
    return entries


def build(task_dir: Path, clone: Path) -> dict:
    """Extract the listed files from the pinned commit into `snapshot/`; write manifest.json."""
    source = load_source(task_dir)
    snapshot = task_dir / "snapshot"
    if snapshot.exists():
        raise SnapshotError(f"{snapshot} exists; remove it deliberately before rebuilding")
    head = _git(clone, "rev-parse", "--verify", source["commit"] + "^{commit}").decode().strip()
    if head != source["commit"]:
        raise SnapshotError(f"clone does not contain commit {source['commit']}")
    entries = _blob_entries(clone, source["commit"], source["files"])
    manifest_files: dict[str, dict] = {}
    for rel in source["files"]:
        mode, blob = entries[rel]
        data = _git(clone, "cat-file", "blob", blob)
        target = snapshot / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o755 if mode == MODE_EXEC else 0o644)
        manifest_files[rel] = {
            "git_mode": mode,
            "git_blob": blob,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    manifest = {
        "repository": source["repository"],
        "commit": source["commit"],
        "license": source.get("license"),
        "adaptation": source.get("adaptation"),
        "files": manifest_files,
    }
    (task_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def verify(task_dir: Path, clone: Path | None = None) -> list[str]:
    """Problems found comparing snapshot/ with manifest.json (and the clone when given)."""
    source = load_source(task_dir)
    manifest_path = task_dir / "manifest.json"
    if not manifest_path.is_file():
        return ["manifest.json missing"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if manifest.get("commit") != source["commit"]:
        problems.append("manifest commit differs from task.json source.commit")
    if sorted(manifest.get("files", {})) != sorted(source["files"]):
        problems.append("manifest file list differs from task.json source.files")
    snapshot = task_dir / "snapshot"
    tree = inspect_tree(snapshot)
    if not tree.ok:
        problems.append("snapshot tree: " + tree.problems())
    extra = sorted(set(tree.files) - set(manifest.get("files", {})))
    if extra:
        problems.append(f"files in snapshot not in manifest: {extra}")
    for rel, entry in manifest.get("files", {}).items():
        found = tree.files.get(rel)
        if found is None:
            problems.append(f"missing from snapshot: {rel}")
            continue
        if found.sha256 != entry["sha256"]:
            problems.append(f"content differs from manifest: {rel}")
        executable = os.access(snapshot / rel, os.X_OK)
        if executable != (entry["git_mode"] == MODE_EXEC):
            problems.append(f"executable bit differs from git mode: {rel}")
    if clone is not None and manifest.get("adaptation") is None:
        try:
            entries = _blob_entries(clone, manifest["commit"], list(manifest["files"]))
        except SnapshotError as error:
            problems.append(f"clone check: {error}")
        else:
            for rel, (mode, blob) in entries.items():
                entry = manifest["files"][rel]
                if (mode, blob) != (entry["git_mode"], entry["git_blob"]):
                    problems.append(f"manifest differs from commit {manifest['commit']}: {rel}")
                data = _git(clone, "cat-file", "blob", blob)
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    problems.append(f"sha256 differs from upstream blob: {rel}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", required=True, help="task directory name under coding_tasks/")
    parser.add_argument("--clone", type=Path, default=None, help="path of the canonical clone")
    parser.add_argument("--build", action="store_true", help="extract snapshot/ and manifest")
    args = parser.parse_args(argv)
    task_dir = Path(__file__).parent / "coding_tasks" / args.task
    try:
        if args.build:
            if args.clone is None:
                raise SnapshotError("--build needs --clone")
            manifest = build(task_dir, args.clone)
            print(f"built {len(manifest['files'])} files at commit {manifest['commit']}")
        problems = verify(task_dir, args.clone)
    except SnapshotError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"snapshot verified against manifest{' and clone' if args.clone else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
