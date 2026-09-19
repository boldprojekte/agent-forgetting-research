"""Trusted first process INSIDE the sandbox. Standard library only; never imports the package.

The harness starts this script as the bwrap command with:
- fd 0: one JSON job (`argv`, `stdin`, `export`, `manifest`, `limits`);
- fd 1 / fd 2: passed through unchanged to the task process (its output);
- the descriptor named in argv[1]: the export channel back to the harness. The script makes
  itself non-dumpable first, so the task process (same uid, a descendant) cannot open
  `/proc/<pid>/fd/<n>`; it never inherits the descriptor either (close_fds).

Steps: copy the read-only host tree `/src` into the tmpfs `/work` (regular files only,
permission bits kept), run the task process (`argv` as given, no shell) with cwd `/work`,
SIGKILL every other process in the pid namespace and confirm none is left, then (if asked)
stream the regular files whose content differs from the input manifest as framed records: a
JSON header line followed by exactly `size` raw bytes. The last line is a `done` record; the
harness treats its absence as a failed run. Bounds on entries, bytes and per-file size stop
the export with an error recorded in the `done` record; the harness then applies nothing.
"""

import ctypes
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time

SRC = "/src"
WORK = "/work"
MAX_JOB_BYTES = 8_000_000
KILL_WAIT_S = 3.0
PR_SET_DUMPABLE = 4


def main() -> int:
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    channel = os.fdopen(int(sys.argv[1]), "wb", closefd=True)
    report = {"type": "done"}
    try:
        job = json.loads(sys.stdin.buffer.read(MAX_JOB_BYTES))
        report["import"] = import_tree()
        report["exit"] = run_task(job)
        report["killed_in_sandbox"], report["survivors"] = kill_others()
        if job.get("export"):
            report["export"] = export_tree(job, channel)
    except Exception as error:  # noqa: BLE001 - everything becomes a recorded failure
        report["error"] = f"{type(error).__name__}: {error}"[:500]
    channel.write(json.dumps(report).encode("utf-8") + b"\n")
    channel.flush()
    return 0


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "device" if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) else "unknown"


def _walk(root: str):
    """Yield (relative path, kind, lstat) in sorted order without following symlinks."""
    stack = [root]
    while stack:
        directory = stack.pop()
        for name in sorted(os.listdir(directory), reverse=True):
            path = os.path.join(directory, name)
            st = os.lstat(path)
            kind = _kind(st.st_mode)
            if kind == "dir":
                stack.append(path)
            yield os.path.relpath(path, root), kind, st


def import_tree() -> dict:
    skipped: dict[str, int] = {}
    count = 0
    for rel, kind, st in _walk(SRC):
        target = os.path.join(WORK, rel)
        if kind == "dir":
            os.makedirs(target, exist_ok=True)
        elif kind == "file":
            with open(os.path.join(SRC, rel), "rb") as src, open(target, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            os.chmod(target, st.st_mode & 0o777)  # scripts stay executable
            count += 1
        else:
            skipped[kind] = skipped.get(kind, 0) + 1
    return {"files": count, "skipped": skipped}


def run_task(job: dict) -> int:
    proc = subprocess.Popen(
        job["argv"],
        cwd=WORK,
        stdin=subprocess.PIPE,
        close_fds=True,
    )
    proc.communicate(job.get("stdin", "").encode("utf-8"))
    return proc.returncode


def _others() -> list[int]:
    me = os.getpid()
    return [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) not in (1, me)]


def kill_others() -> tuple[int, int]:
    """SIGKILL everything else in the pid namespace; return (killed, survivors after wait)."""
    before = len(_others())
    deadline = time.monotonic() + KILL_WAIT_S
    while True:
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass
        remaining = _others()
        if not remaining or time.monotonic() > deadline:
            return before, len(remaining)
        time.sleep(0.02)


def export_tree(job: dict, channel) -> dict:
    limits = job["limits"]
    manifest = job["manifest"]
    present: list[str] = []
    skipped: dict[str, int] = {}
    entries = 0
    total = 0
    result = {"entries": 0, "bytes": 0, "present": present, "skipped": skipped, "error": None}
    for rel, kind, st in _walk(WORK):
        if kind == "dir":
            continue
        if kind != "file":
            skipped[kind] = skipped.get(kind, 0) + 1
            continue
        present.append(rel)
        if len(present) > limits["present_entries"]:
            result["error"] = f"more than {limits['present_entries']} entries in the workspace"
            break
        if st.st_size > limits["file_bytes"]:
            result["error"] = f"file too large to export: {rel} ({st.st_size} bytes)"
            break
        path = os.path.join(WORK, rel)
        with open(path, "rb") as handle:
            data = handle.read(limits["file_bytes"] + 1)
        if len(data) != st.st_size:
            result["error"] = f"file changed while exporting: {rel}"
            break
        digest = hashlib.sha256(data).hexdigest()
        if manifest.get(rel) == digest:
            continue
        total += len(data)
        if total > limits["bytes"]:
            result["error"] = f"export exceeds {limits['bytes']} bytes"
            break
        entries += 1
        if entries > limits["entries"]:
            result["error"] = f"more than {limits['entries']} changed file entries"
            break
        header = {"type": "file", "path": rel, "size": len(data), "sha256": digest}
        channel.write(json.dumps(header).encode("utf-8") + b"\n")
        channel.write(data)
    result["entries"] = entries
    result["bytes"] = total
    return result


if __name__ == "__main__":
    sys.exit(main())
