"""OS-level isolation for executing agent-written code. Fail closed; no host fallback.

Backend: bubblewrap (`bwrap`) started inside a transient systemd *user* scope.

- bwrap gives fresh user/pid/net/ipc/uts/cgroup namespaces (`--unshare-all`), a read-only
  runtime (`/usr`, the project virtualenv and its interpreter at their own paths), the host
  workspace read-only at `/src`, a size-limited tmpfs at `/work` (the only writable tree, cwd of
  the task), a tmpfs `/tmp`, no `/home`, no `/etc`, an empty environment, `--die-with-parent`
  and `--new-session`. Rootless, no capabilities, no daemon, no socket.
- The systemd scope carries the cgroup v2 limits bwrap cannot set: `TasksMax` (pids) and
  `MemoryMax` (+ `MemorySwapMax=0`). tmpfs pages and inodes are charged to that cgroup, so
  the memory limit also bounds the number of files; `--size` bounds the bytes. Killing the
  scope kills the whole process tree.
- The first process in the namespace is the trusted `sandbox_bootstrap.py`: it copies `/src`
  into `/work`, runs the task process, kills every other process in the namespace, and streams
  changed regular files back over a private pipe (fd 3). The harness validates every record
  (relative plain path, size, sha256, entry and byte bounds) and only then writes into the host
  workspace. Task code never has a writable host mount.
- The harness enforces a wall-clock timeout, bounds captured output, and confirms after every
  run that the scope is gone before it touches the workspace.
- `Sandbox.run` takes a full argv (program plus arguments, never a shell string); `run_python`
  prefixes the sandboxed interpreter. Programs come from the read-only runtime or from
  `/work`, where the bootstrap keeps the permission bits of imported files (shell scripts
  stay executable). Exported files keep the mode of the host file they replace.

`preflight()` verifies every prerequisite with real runs and returns a report. `Sandbox`
refuses to run unless the report is ok. Nothing in this module ever executes task code
outside the namespace, and a run without a trusted bootstrap report is a recorded failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .tree import DEFAULT_MAX_ENTRIES, has_symlink_component, inspect_tree, safe_relative

WORK_MOUNT = "/work"
SRC_MOUNT = "/src"
BOOTSTRAP_MOUNT = "/fa/bootstrap.py"
BOOTSTRAP_PATH = Path(__file__).with_name("sandbox_bootstrap.py")
BACKEND = "bwrap+systemd-user-scope+tmpfs-work"

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MEMORY_MB = 512
DEFAULT_MAX_PIDS = 64
DEFAULT_MAX_OUTPUT_BYTES = 64_000
DEFAULT_WORK_MB = 64
# A runaway writer is killed once it has produced this many times the kept output.
OUTPUT_KILL_FACTOR = 8

# Export bounds (what may come back from one run) and import bounds (what may go in).
EXPORT_MAX_ENTRIES = 500
EXPORT_MAX_BYTES = 8_000_000
EXPORT_MAX_FILE_BYTES = 2_000_000
IMPORT_MAX_ENTRIES = DEFAULT_MAX_ENTRIES

CLEANUP_WAIT_S = 10.0
SCOPE_STOP_WAIT_S = 5.0

PREFLIGHT_MEMORY_MB = 64
PREFLIGHT_MAX_PIDS = 8
PREFLIGHT_INODE_MEMORY_MB = 32
PREFLIGHT_INODE_FILES = 400_000

MB = 1024 * 1024


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    """Result of the prerequisite verification; `ok` only if every check passed."""

    backend: str
    checks: tuple[Check, ...]
    python: str  # interpreter path used inside the sandbox
    ro_paths: tuple[str, ...]  # host paths bound read-only at the same location

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def summary(self) -> str:
        return "; ".join(f"{c.name}: {'ok' if c.ok else 'FAIL'} ({c.detail})" for c in self.checks)

    def describe(self) -> dict:
        return {"backend": self.backend, "ok": self.ok, **asdict(self)}


@dataclass(frozen=True)
class ChangeSet:
    """Outcome of importing the run's exported files into the host workspace."""

    applied: bool
    written: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    skipped: dict = field(default_factory=dict)  # non-regular entries left in the sandbox
    error: str | None = None

    def describe(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int | None  # the task process; None when it did not finish normally
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_s: float
    killed: str | None = None  # reason when the harness or a limit killed the task
    error: str | None = None  # the run did not complete as a trusted, verified run
    changes: ChangeSet | None = None  # None when no export was requested
    cleanup: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        """The task process finished on its own and the bootstrap reported it."""
        return (
            self.error is None
            and not self.timed_out
            and self.killed is None
            and self.exit_code is not None
        )

    def describe(self) -> dict:
        data = asdict(self)
        data["completed"] = self.completed
        return data


def _interpreter_paths() -> tuple[str, list[str]]:
    """The venv python plus the host paths that must be visible (read-only) to run it."""
    venv = Path(sys.prefix).resolve()
    python = venv / "bin" / "python"
    real = Path(os.path.realpath(python))
    install_root = real.parents[1]  # .../cpython-x.y.z-.../bin/python3.11 -> install root
    paths = {str(venv), str(install_root)}
    cfg = venv / "pyvenv.cfg"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("home"):
                home = Path(line.split("=", 1)[1].strip())
                paths.add(str(home.parent))  # may be a symlink to install_root; bind both
    return str(python), sorted(paths)


def _bwrap_base(ro_paths: list[str]) -> list[str]:
    argv = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PYTHONUNBUFFERED", "1",
    ]  # fmt: skip
    for path in ro_paths:
        argv += ["--ro-bind", path, path]
    return argv


def _bwrap_work(host_workspace: Path, work_mb: int) -> list[str]:
    """Host tree read-only at /src, bounded tmpfs at /work, the bootstrap read-only."""
    return [
        "--ro-bind", str(host_workspace), SRC_MOUNT,
        "--size", str(work_mb * MB), "--tmpfs", WORK_MOUNT,
        "--ro-bind", str(BOOTSTRAP_PATH), BOOTSTRAP_MOUNT,
    ]  # fmt: skip


def _bwrap_tail(chdir: str) -> list[str]:
    """After every bind: make the root read-only (binds keep their own mode) and set cwd."""
    return ["--remount-ro", "/", "--chdir", chdir, "--"]


def _scope_prefix(unit: str, memory_mb: int, max_pids: int) -> list[str]:
    """Transient user scope with the cgroup limits.

    `OOMPolicy=continue`: with the default `stop`, systemd terminates the whole scope
    (SIGTERM) after the kernel OOM-kills one process, which races with the bootstrap's report.
    With `continue` the task process dies (exit 137 / SIGKILL) and the bootstrap reports it.
    """
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "--unit", unit,
        "-p", f"MemoryMax={memory_mb}M",
        "-p", "MemorySwapMax=0",
        "-p", f"TasksMax={max_pids}",
        "-p", "OOMPolicy=continue",
        "--",
    ]  # fmt: skip


def preflight() -> PreflightReport:
    """Verify every prerequisite with real runs. Never raises; the report says what failed."""
    checks: list[Check] = []
    python, ro_paths = _interpreter_paths()

    bwrap = shutil.which("bwrap")
    checks.append(Check("bwrap", bwrap is not None, bwrap or "bwrap not on PATH"))
    sd = shutil.which("systemd-run")
    ctl = shutil.which("systemctl")
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    sd_ok = bool(sd and ctl and runtime_dir)
    checks.append(
        Check(
            "systemd_run",
            sd_ok,
            f"systemd-run={sd}, systemctl={ctl}, XDG_RUNTIME_DIR={runtime_dir or 'unset'}",
        )
    )
    missing = [p for p in ro_paths if not Path(p).is_dir()]
    checks.append(
        Check(
            "interpreter",
            Path(python).exists() and not missing and BOOTSTRAP_PATH.is_file(),
            f"python={python}, ro_paths={ro_paths}" + (f", missing={missing}" if missing else ""),
        )
    )
    if not all(c.ok for c in checks):
        return PreflightReport(BACKEND, tuple(checks), python, tuple(ro_paths))

    # A namespace really starts and sees no network.
    probe = (
        "import os, socket, sys\n"
        "assert os.getcwd() == '/tmp', os.getcwd()\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=1)\n"
        "except OSError:\n"
        "    print('namespace-ok')\n"
        "else:\n"
        "    print('NETWORK-OPEN'); sys.exit(3)\n"
    )
    result = _capture(
        [*_bwrap_base(ro_paths), *_bwrap_tail("/tmp"), python, "-c", probe], timeout=30
    )
    checks.append(
        Check(
            "namespace",
            result.returncode == 0 and "namespace-ok" in result.stdout,
            _tail(result),
        )
    )

    # A transient user scope really carries the cgroup limits.
    unit = f"fa-preflight-{secrets.token_hex(4)}"
    shell = (
        "cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); "
        "echo pids.max=$(cat $cg/pids.max) memory.max=$(cat $cg/memory.max)"
    )
    result = _capture(
        [*_scope_prefix(unit, PREFLIGHT_MEMORY_MB, PREFLIGHT_MAX_PIDS), "/bin/sh", "-c", shell],
        timeout=30,
    )
    expected = f"pids.max={PREFLIGHT_MAX_PIDS} memory.max={PREFLIGHT_MEMORY_MB * MB}"
    checks.append(
        Check("scope_limits", result.returncode == 0 and expected in result.stdout, _tail(result))
    )

    # The /work tmpfs really refuses writes beyond its size.
    with tempfile.TemporaryDirectory(prefix="fa-preflight-") as tmp:
        probe = (
            "import os\n"
            "try:\n"
            "    with open('/work/big', 'wb') as f:\n"
            "        for _ in range(3): f.write(b'x' * 1048576)\n"
            "    print('UNBOUNDED')\n"
            "except OSError as e:\n"
            "    print('enospc' if e.errno == 28 else f'errno {e.errno}')\n"
        )
        result = _capture(
            [
                *_bwrap_base(ro_paths),
                *_bwrap_work(Path(tmp), 1),
                *_bwrap_tail(WORK_MOUNT),
                python,
                "-c",
                probe,
            ],  # fmt: skip
            timeout=30,
        )
        checks.append(
            Check("work_tmpfs", result.returncode == 0 and "enospc" in result.stdout, _tail(result))
        )

        # tmpfs inodes are charged to the memory cgroup: a flood of empty files is stopped.
        probe = (
            "import os\n"
            "os.mkdir('/work/m')\n"
            f"for i in range({PREFLIGHT_INODE_FILES}):\n"
            "    open(f'/work/m/{i}', 'w').close()\n"
            "print('CREATED-ALL')\n"
        )
        unit = f"fa-preflight-{secrets.token_hex(4)}"
        result = _capture(
            [
                *_scope_prefix(unit, PREFLIGHT_INODE_MEMORY_MB, PREFLIGHT_MAX_PIDS),
                *_bwrap_base(ro_paths),
                *_bwrap_work(Path(tmp), DEFAULT_WORK_MB),
                *_bwrap_tail(WORK_MOUNT),
                python,
                "-c",
                probe,
            ],  # fmt: skip
            timeout=120,
        )
        stopped = result.returncode != 0 and "CREATED-ALL" not in result.stdout
        checks.append(
            Check(
                "tmpfs_inodes",
                stopped,
                f"exit {result.returncode}, {PREFLIGHT_INODE_FILES} empty files under "
                f"MemoryMax={PREFLIGHT_INODE_MEMORY_MB}M: "
                + ("stopped" if stopped else "NOT stopped: " + _tail(result)),
            )
        )

        # The full protocol round-trips: bootstrap, task output, kill, export, host import.
        if all(c.ok for c in checks):
            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "old.txt").write_text("old\n", encoding="utf-8")
            outcome = _run(
                python,
                ro_paths,
                ws,
                [python, "-c", "open('made.txt', 'w').write('made'); print('bootstrap-ok')"],
                timeout_s=30,
                memory_mb=PREFLIGHT_MEMORY_MB,
                max_pids=PREFLIGHT_MAX_PIDS,
                max_output_bytes=4096,
                work_mb=4,
                stdin_text="",
                export=True,
            )
            round_trip = (
                outcome.completed
                and outcome.exit_code == 0
                and "bootstrap-ok" in outcome.stdout
                and outcome.changes is not None
                and outcome.changes.applied
                and outcome.changes.written == ("made.txt",)
                and (ws / "made.txt").read_text(encoding="utf-8") == "made"
                and outcome.cleanup.get("scope_stopped") is True
            )
            checks.append(
                Check(
                    "bootstrap",
                    round_trip,
                    "export round-trip ok"
                    if round_trip
                    else f"error={outcome.error} changes={outcome.changes} "
                    f"cleanup={outcome.cleanup} stderr={outcome.stderr[:200]}",
                )
            )
    return PreflightReport(BACKEND, tuple(checks), python, tuple(ro_paths))


def _capture(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 1, "", f"{type(error).__name__}: {error}")


def _tail(result: subprocess.CompletedProcess) -> str:
    return (result.stdout + result.stderr).strip()[:300] or f"exit {result.returncode}"


class SandboxUnavailable(RuntimeError):
    """Raised instead of running anything when the preflight did not pass."""


class Sandbox:
    def __init__(self, report: PreflightReport) -> None:
        if not report.ok:
            raise SandboxUnavailable("sandbox preflight failed: " + report.summary())
        self.report = report

    def run_python(
        self,
        workspace: Path,
        args: list[str],
        timeout_s: float = DEFAULT_TIMEOUT_S,
        memory_mb: int = DEFAULT_MEMORY_MB,
        max_pids: int = DEFAULT_MAX_PIDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        work_mb: int = DEFAULT_WORK_MB,
        stdin_text: str = "",
        export: bool = True,
    ) -> RunOutcome:
        """Run the sandboxed interpreter with `args` on a tmpfs copy of `workspace`."""
        return self.run(
            workspace,
            [self.report.python, *args],
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            max_pids=max_pids,
            max_output_bytes=max_output_bytes,
            work_mb=work_mb,
            stdin_text=stdin_text,
            export=export,
        )

    def run(
        self,
        workspace: Path,
        argv: list[str],
        timeout_s: float = DEFAULT_TIMEOUT_S,
        memory_mb: int = DEFAULT_MEMORY_MB,
        max_pids: int = DEFAULT_MAX_PIDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        work_mb: int = DEFAULT_WORK_MB,
        stdin_text: str = "",
        export: bool = True,
    ) -> RunOutcome:
        """Run `argv` (program plus arguments, no shell) with cwd on a tmpfs copy of `workspace`.

        The program is resolved inside the namespace (read-only `/usr`, or a path in `/work`).
        With `export=True` the regular files the run changed, added or deleted are imported
        back into `workspace` after the run was verified to be over; `outcome.changes` says
        what happened. `stdin_text` is fed to the task process.
        """
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise FileNotFoundError(f"workspace is not a directory: {workspace}")
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        return _run(
            self.report.python,
            list(self.report.ro_paths),
            workspace,
            argv,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            max_pids=max_pids,
            max_output_bytes=max_output_bytes,
            work_mb=work_mb,
            stdin_text=stdin_text,
            export=export,
        )


def _run(
    python: str,
    ro_paths: list[str],
    workspace: Path,
    argv_task: list[str],
    *,
    timeout_s: float,
    memory_mb: int,
    max_pids: int,
    max_output_bytes: int,
    work_mb: int,
    stdin_text: str,
    export: bool,
) -> RunOutcome:
    limits = {
        "timeout_s": timeout_s,
        "memory_mb": memory_mb,
        "max_pids": max_pids,
        "max_output_bytes": max_output_bytes,
        "work_mb": work_mb,
        "export_max_entries": EXPORT_MAX_ENTRIES,
        "workspace_max_entries": IMPORT_MAX_ENTRIES,
        "export_max_bytes": EXPORT_MAX_BYTES,
        "export_max_file_bytes": EXPORT_MAX_FILE_BYTES,
    }
    started = time.monotonic()

    def refused(reason: str) -> RunOutcome:
        return RunOutcome(
            exit_code=None,
            stdout="",
            stderr="",
            truncated=False,
            timed_out=False,
            duration_s=round(time.monotonic() - started, 3),
            error=f"workspace refused before any run: {reason}",
            changes=ChangeSet(False, error="nothing ran") if export else None,
            cleanup={"scope_stopped": True, "detail": "no process started"},
            limits=limits,
        )

    tree = inspect_tree(workspace, IMPORT_MAX_ENTRIES)
    if not tree.ok:
        return refused(tree.problems())
    if tree.bytes > work_mb * MB // 2:
        return refused(f"{tree.bytes} bytes exceed half the {work_mb} MB work tmpfs")
    manifest = {rel: entry.sha256 for rel, entry in tree.files.items()}
    job = {
        "argv": list(argv_task),
        "stdin": stdin_text,
        "export": export,
        "manifest": manifest,
        "limits": {
            "entries": EXPORT_MAX_ENTRIES,
            "present_entries": IMPORT_MAX_ENTRIES,
            "bytes": EXPORT_MAX_BYTES,
            "file_bytes": EXPORT_MAX_FILE_BYTES,
        },
    }
    unit = f"fa-sbx-{secrets.token_hex(6)}"
    argv = [
        *_scope_prefix(unit, memory_mb, max_pids),
        *_bwrap_base(ro_paths),
        *_bwrap_work(workspace, work_mb),
        *_bwrap_tail(WORK_MOUNT),
    ]  # fmt: skip
    channel_r, channel_w = os.pipe()
    # systemd-run and bwrap pass the inherited descriptor through unchanged; the bootstrap is
    # the only process that sees it (close_fds for the task, dumpable=0 for its /proc entry).
    argv += [python, "-I", "-u", BOOTSTRAP_MOUNT, str(channel_w)]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(channel_w,),
        )
    finally:
        os.close(channel_w)
    out = _Reader(proc.stdout, max_output_bytes)
    err = _Reader(proc.stderr, max_output_bytes)
    export_limit = EXPORT_MAX_BYTES + IMPORT_MAX_ENTRIES * 4096 + 1 * MB
    channel = _Reader(os.fdopen(channel_r, "rb"), export_limit)
    _feed(proc.stdin, json.dumps(job).encode("utf-8"))
    killed: str | None = None
    timed_out = False
    deadline = started + timeout_s
    while True:
        if proc.poll() is not None:
            break
        if time.monotonic() > deadline:
            timed_out = True
            killed = f"timeout after {timeout_s}s"
            break
        if out.total + err.total > OUTPUT_KILL_FACTOR * max_output_bytes:
            killed = "output limit exceeded"
            break
        if channel.total > export_limit:
            killed = "export channel limit exceeded"
            break
        time.sleep(0.02)
    cleanup = _stop(unit, proc, forced=killed is not None)
    out.join()
    err.join()
    channel.join()
    files, done, protocol_error = _parse_channel(channel.data())
    error: str | None = None
    exit_code: int | None = None
    if not cleanup["scope_stopped"]:
        error = "cleanup unverified: " + cleanup["detail"]
    elif killed is not None:
        pass
    elif done is None:
        error = (
            f"sandbox run did not complete: no trusted bootstrap report (bwrap exit "
            f"{proc.returncode}{', ' + protocol_error if protocol_error else ''})"
        )
    elif done.get("error"):
        error = "bootstrap failed: " + str(done["error"])
    elif protocol_error:
        error = "export channel corrupt: " + protocol_error
    else:
        exit_code = done.get("exit")
        if not isinstance(exit_code, int):
            exit_code = None
            error = "bootstrap report has no exit code"
        elif exit_code == 137:
            killed = "killed by the sandbox (exit 137, usually the memory limit)"
        elif exit_code < 0:
            killed = f"killed by signal {-exit_code} (usually the memory limit)"
    if done is not None:
        cleanup["killed_in_sandbox"] = done.get("killed_in_sandbox")
        cleanup["survivors"] = done.get("survivors")
        if cleanup["survivors"] not in (0, None) and error is None:
            error = f"{cleanup['survivors']} process(es) survived the in-sandbox kill"
    changes: ChangeSet | None = None
    if export:
        if error is not None or killed is not None:
            changes = ChangeSet(False, error="run not completed; nothing imported")
        else:
            changes = _apply_export(workspace, files, done.get("export"), manifest)
    return RunOutcome(
        exit_code=None if (timed_out or error) else exit_code,
        stdout=out.text(),
        stderr=err.text(),
        truncated=out.truncated or err.truncated,
        timed_out=timed_out,
        duration_s=round(time.monotonic() - started, 3),
        killed=killed,
        error=error,
        changes=changes,
        cleanup=cleanup,
        limits=limits,
    )


def _feed(pipe, data: bytes) -> None:
    """Write the job to the bootstrap's stdin on a thread (it may exit before reading)."""

    def write() -> None:
        try:
            pipe.write(data)
            pipe.close()
        except (BrokenPipeError, OSError):
            pass

    threading.Thread(target=write, daemon=True).start()


def _stop(unit: str, proc: subprocess.Popen, forced: bool) -> dict:
    """Bounded kill and wait; confirm the scope (the whole cgroup) is gone."""
    detail: list[str] = []
    if forced:
        result = _capture(
            ["systemctl", "--user", "kill", "--signal=KILL", f"{unit}.scope"], timeout=10
        )
        detail.append(f"kill scope: exit {result.returncode}")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=CLEANUP_WAIT_S)
    except subprocess.TimeoutExpired:
        detail.append("bwrap did not exit within the cleanup wait")
    stopped = False
    deadline = time.monotonic() + SCOPE_STOP_WAIT_S
    while True:
        result = _capture(
            ["systemctl", "--user", "show", "-p", "ActiveState", "--value", f"{unit}.scope"],
            timeout=5,
        )
        state = result.stdout.strip()
        if result.returncode == 0 and state in ("inactive", "failed", ""):
            stopped = proc.poll() is not None
            break
        if time.monotonic() > deadline:
            detail.append(f"scope still {state or 'unknown'} after {SCOPE_STOP_WAIT_S}s")
            break
        time.sleep(0.05)
    if not stopped and proc.poll() is None:
        detail.append("bwrap still running")
    return {"scope_stopped": stopped, "detail": "; ".join(detail) or "scope inactive"}


def _parse_channel(data: bytes) -> tuple[list[tuple[str, bytes]], dict | None, str | None]:
    """Split the export stream into (file records, done record, protocol error)."""
    files: list[tuple[str, bytes]] = []
    pos = 0
    while pos < len(data):
        end = data.find(b"\n", pos)
        if end < 0:
            return files, None, "unterminated record"
        try:
            header = json.loads(data[pos:end])
        except (json.JSONDecodeError, UnicodeDecodeError):
            return files, None, "malformed record header"
        pos = end + 1
        if not isinstance(header, dict):
            return files, None, "record is not an object"
        kind = header.get("type")
        if kind == "done":
            return files, header, "data after the done record" if pos < len(data) else None
        if kind != "file":
            return files, None, f"unknown record type {kind!r}"
        size = header.get("size")
        if not isinstance(size, int) or size < 0 or pos + size > len(data):
            return files, None, "record size does not match the stream"
        body = data[pos : pos + size]
        pos += size
        path = header.get("path")
        if sha256_bytes(body) != header.get("sha256"):
            return files, None, f"sha256 mismatch for {path!r}"
        files.append((path, body))
    return files, None, "stream ended without a done record"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _apply_export(
    root: Path, files: list[tuple[str, bytes]], export: object, manifest: dict[str, str]
) -> ChangeSet:
    """Validate the whole export, then write it into the host workspace. All or nothing."""
    if not isinstance(export, dict):
        return ChangeSet(False, error="bootstrap report has no export section")
    skipped = export.get("skipped") or {}
    if export.get("error"):
        return ChangeSet(False, skipped=skipped, error=str(export["error"]))
    present_raw = export.get("present")
    if not isinstance(present_raw, list) or len(present_raw) > IMPORT_MAX_ENTRIES:
        return ChangeSet(False, skipped=skipped, error="export listing missing or too long")
    if len(files) > EXPORT_MAX_ENTRIES or sum(len(b) for _, b in files) > EXPORT_MAX_BYTES:
        return ChangeSet(False, skipped=skipped, error="export exceeds entry or byte bounds")
    present: set[str] = set()
    for raw in present_raw:
        rel = safe_relative(raw)
        if rel is None:
            return ChangeSet(False, skipped=skipped, error=f"unsafe path in export: {raw!r}")
        present.add(rel)
    writes: list[tuple[str, bytes]] = []
    for raw, body in files:
        rel = safe_relative(raw)
        if rel is None or rel not in present:
            return ChangeSet(False, skipped=skipped, error=f"unsafe path in export: {raw!r}")
        if len(body) > EXPORT_MAX_FILE_BYTES:
            return ChangeSet(False, skipped=skipped, error=f"file too large: {rel}")
        if has_symlink_component(root, rel):
            return ChangeSet(False, skipped=skipped, error=f"path through a symlink: {rel}")
        writes.append((rel, body))
    deletions = [rel for rel in manifest if rel not in present]
    for rel in deletions:
        if has_symlink_component(root, rel):
            return ChangeSet(False, skipped=skipped, error=f"path through a symlink: {rel}")
    for rel, body in writes:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".fa-export-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, _mode_of(target, 0o644))  # keep an existing file's mode (exec bit)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    for rel in deletions:
        target = root / rel
        if target.is_file() and not target.is_symlink():
            target.unlink()
    return ChangeSet(
        True,
        written=tuple(rel for rel, _ in writes),
        deleted=tuple(deletions),
        skipped=skipped,
    )


def _mode_of(path: Path, default: int) -> int:
    """Permission bits of an existing regular file, else `default`."""
    try:
        return os.lstat(path).st_mode & 0o777
    except FileNotFoundError:
        return default


class _Reader:
    """Drain a pipe on a thread; keep the first `limit` bytes, count everything."""

    def __init__(self, pipe, limit: int) -> None:
        self._pipe = pipe
        self._limit = limit
        self._kept = bytearray()
        self.total = 0
        self.truncated = False
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while True:
            chunk = self._pipe.read(65536)
            if not chunk:
                return
            self.total += len(chunk)
            room = self._limit - len(self._kept)
            if room > 0:
                self._kept += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def join(self) -> None:
        self._thread.join(timeout=5)

    def data(self) -> bytes:
        return bytes(self._kept)

    def text(self) -> str:
        text = bytes(self._kept).decode("utf-8", errors="replace")
        if self.truncated:
            text += f"\n[output truncated: {self.total} bytes produced, {self._limit} kept]"
        return text


__all__ = [
    "BACKEND",
    "ChangeSet",
    "Check",
    "EXPORT_MAX_BYTES",
    "EXPORT_MAX_ENTRIES",
    "EXPORT_MAX_FILE_BYTES",
    "PreflightReport",
    "RunOutcome",
    "Sandbox",
    "SandboxUnavailable",
    "preflight",
]
