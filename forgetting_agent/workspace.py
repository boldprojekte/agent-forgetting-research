"""Coding task tools over a disposable per-run workspace, plus their model-facing schemas.

A `Workspace` is created from a pristine snapshot directory: the snapshot is copied twice into
the run directory, `snapshot/` (never modified, used by `diff`) and `workspace/` (what the
tools and the sandboxed test runner see). Copies take directories and regular files only
(`tree.copy_tree`): symlinks are skipped, FIFOs/sockets/devices make the copy refuse. Nothing
outside `workspace/` is ever read or written by a tool: paths must be relative, must not
contain `..`, and no component may be a symlink (`os.path.realpath` of the joined path must
equal the joined path). Non-regular entries are hidden from listing and search and reported
by `changes`.

Everything that reads workspace data is bounded, because the sandbox can put arbitrary bytes
there: file size for read/edit/context (`READ_MAX_FILE_BYTES`), line length in read output,
ripgrep output bytes (`GREP_MAX_OUTPUT_BYTES`, refused with a hint to narrow), tree entries
(`max_entries`, listing reports truncation), diff size per file and in total.

Tools (all return `ToolResult`; failures are results with the fix, never exceptions):
- list_files, read_file (line window with numbers and an explicit continuation hint),
- grep (ripgrep, literal by default, optional regex, path glob, context lines, bounded and
  paginated by `offset`),
- edit_file (exact unique match, refused when the file changed since the last read, atomic
  replace, returns the unified diff), write_file (new files only), remove_file (own new files),
  diff (typed change list
  against the snapshot plus unified diffs; binary and unsupported entries are named, never
  hidden),
- run_tests (a validated selection, executed only inside the sandbox; files the run changed
  come back through the sandbox's validated export and are listed; a run without a trusted
  completion is an error result). What a selection may name is fixed by the task's
  `TestSpec`: `pytest` (default) takes pytest node ids; `shell` takes paths of the task's test
  scripts (`scripts` glob) or its suite runner (`suite`), each run as `bash <path>`. The
  model never supplies a command line, options or shell syntax in either kind.

The corpus experiment keeps its own tools in tools.py; this module does not change them.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sandbox import Sandbox, SandboxUnavailable
from .tools import ToolResult, load_tool_description
from .tree import DEFAULT_MAX_ENTRIES, FileEntry, copy_tree, inspect_tree

CODING_TOOL_NAMES = (
    "list_files",
    "read_file",
    "grep",
    "edit_file",
    "write_file",
    "remove_file",
    "diff",
    "run_tests",
)

READ_DEFAULT_LINES = 200
READ_MAX_LINES = 500
READ_MAX_FILE_BYTES = 1_000_000
READ_LINE_CHARS = 400
GREP_DEFAULT_RESULTS = 50
GREP_MAX_RESULTS = 200
GREP_MAX_CONTEXT = 5
GREP_LINE_CHARS = 300
GREP_RG_TIMEOUT_S = 20
GREP_MAX_OUTPUT_BYTES = 4_000_000  # ripgrep JSON stream; beyond this the search is refused
DIFF_MAX_CHARS = 60_000
DIFF_MAX_FILE_BYTES = READ_MAX_FILE_BYTES
TEST_OUTPUT_MAX_CHARS = 20_000
# pytest node id: relative path, optional ::Class::test[param]. No leading '-', no whitespace,
# no shell metacharacters; parameters may hold letters, digits and a few separators.
NODE_ID_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_./-]*(::[A-Za-z0-9_]+(\[[A-Za-z0-9_\-.,= ]*\])?)*$"
)
MAX_TEST_IDS = 20
MAX_SHELL_SCRIPTS = 5  # sequential sandbox runs per call
SHELL_INTERPRETER = "bash"


@dataclass(frozen=True)
class TestSpec:
    """How run_tests turns a model-supplied selection into a fixed sandbox command line.

    kind "pytest": items are pytest node ids under the workspace.
    kind "shell": items are workspace paths matching `scripts` (a single-directory glob such
    as `test/shell.d/*-test.sh`) or equal to `suite`; each runs as `bash <path>`.
    """

    kind: str = "pytest"
    scripts: str | None = None
    suite: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("pytest", "shell"):
            raise ValueError(f"unknown test kind {self.kind!r}")
        if self.kind == "shell" and not (self.scripts or self.suite):
            raise ValueError("shell tests need a scripts glob or a suite path")

    @classmethod
    def from_spec(cls, spec: Any) -> TestSpec:
        if spec is None:
            return cls()
        return cls(
            kind=str(spec.get("kind", "pytest")),
            scripts=spec.get("scripts") or None,
            suite=spec.get("suite") or None,
        )

    def selects(self, item: str) -> bool:
        """True when `item` names the suite or matches the scripts glob (one directory)."""
        if self.suite is not None and item == self.suite:
            return True
        if self.scripts is None:
            return False
        directory, _, pattern = self.scripts.rpartition("/")
        item_dir, _, name = item.rpartition("/")
        return item_dir == directory and bool(name) and fnmatch.fnmatchcase(name, pattern)

    def describe_selection(self) -> str:
        if self.kind == "shell":
            parts = []
            if self.scripts:
                parts.append(f"a test script path matching {self.scripts!r}")
            if self.suite:
                parts.append(f"{self.suite!r} for the whole suite")
            return " or ".join(parts)
        return (
            "relative test paths or pytest node ids, e.g. ['tests'] or "
            "['tests/test_x.py::test_name']"
        )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Change:
    """One entry that differs between the pristine snapshot and the live workspace."""

    path: str
    status: str  # added | modified | binary_modified | deleted | unsupported
    kind: str  # file | symlink | fifo | socket | device | unknown
    before_sha: str | None = None
    after_sha: str | None = None
    before_size: int | None = None
    after_size: int | None = None


class Workspace:
    def __init__(
        self,
        root: Path,
        snapshot: Path,
        sandbox: Sandbox | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        tests: TestSpec | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.snapshot = Path(snapshot).resolve()
        self.sandbox = sandbox
        self.max_entries = max_entries
        self.tests = tests or TestSpec()
        self._test_outputs: dict[str, str] = {}
        self._created: set[str] = set()  # Paths created by this Workspace via write_file.
        self._seen: dict[str, str] = {}  # relative path -> sha256 at last read/edit/write
        if not self.root.is_dir():
            raise FileNotFoundError(f"workspace root is not a directory: {self.root}")

    @classmethod
    def from_snapshot(
        cls,
        source: Path,
        run_dir: Path,
        sandbox: Sandbox | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        tests: TestSpec | None = None,
    ) -> Workspace:
        """Copy `source` to `<run_dir>/snapshot` (pristine) and `<run_dir>/workspace` (live).

        Regular files and directories only; a snapshot with FIFOs, sockets, devices or more
        than `max_entries` entries is refused (ValueError) before anything is created.
        """
        run_dir = Path(run_dir)
        snapshot = run_dir / "snapshot"
        workspace = run_dir / "workspace"
        for target in (snapshot, workspace):
            if target.exists():
                raise FileExistsError(f"{target} already exists; a run directory is used once")
        report = inspect_tree(source, max_entries, hashes=False)
        if not report.ok:
            raise ValueError(f"refusing snapshot {source}: {report.problems()}")
        for target in (snapshot, workspace):
            copy_tree(source, target, max_entries)
        return cls(workspace, snapshot, sandbox, max_entries, tests)

    # -- path safety --------------------------------------------------------------------------

    def _resolve(self, path: str, must_exist: bool = True) -> Path | ToolResult:
        if path.startswith("test-output:"):
            return ToolResult(
                "Captured test output is read-only; use read_file to inspect it.", ok=False
            )
        cleaned = (path or "").strip()
        if not cleaned:
            return ToolResult("Missing path. Call list_files to see workspace paths.", ok=False)
        parts = Path(cleaned).parts
        if Path(cleaned).is_absolute() or ".." in parts:
            return ToolResult(
                f"Path {cleaned!r} must be relative to the workspace root and must not contain "
                "'..'. Use a path as listed by list_files.",
                ok=False,
            )
        joined = self.root / cleaned
        if Path(os.path.realpath(joined)) != joined:
            return ToolResult(
                f"Path {cleaned!r} goes through a symlink and cannot be used. Symlinks are not "
                "accessible; use a regular file path from list_files.",
                ok=False,
            )
        if must_exist and not joined.is_file():
            return ToolResult(
                f"No file {cleaned!r} in the workspace. Call list_files to see valid paths.",
                ok=False,
            )
        return joined

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _safe_files(self) -> tuple[list[str], bool]:
        """Relative paths of regular files reached without any symlink, sorted; truncated flag."""
        report = inspect_tree(self.root, self.max_entries, hashes=False)
        return list(report.files), report.truncated

    def _read_bounded(self, path: Path, rel: str) -> bytes | ToolResult:
        size = path.stat().st_size
        if size > READ_MAX_FILE_BYTES:
            return ToolResult(
                f"{rel!r} is too large to read ({size} bytes; limit {READ_MAX_FILE_BYTES}). "
                "Use grep with a path_glob to inspect it.",
                ok=False,
            )
        with open(path, "rb") as handle:
            return handle.read(READ_MAX_FILE_BYTES + 1)

    # -- listing / reading -----------------------------------------------------------------------

    def list_files(self) -> ToolResult:
        names, truncated = self._safe_files()
        if truncated:
            names.append(f"[listing truncated at {self.max_entries} entries]")
        return ToolResult("\n".join(names) if names else "Workspace is empty.")

    def read_file(
        self, path: str, start_line: int = 1, max_lines: int | None = None, start_column: int = 1
    ) -> ToolResult:
        if path.startswith("test-output:"):
            text = self._test_outputs.get(path)
            if text is None:
                return ToolResult(
                    "Unknown test output. Copy the exact test-output: ID from run_tests.", ok=False
                )
            rel = path
        else:
            resolved = self._resolve(path)
            if isinstance(resolved, ToolResult):
                return resolved
            rel = self._relative(resolved)
            data = self._read_bounded(resolved, rel)
            if isinstance(data, ToolResult):
                return data
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return ToolResult(f"File {path!r} is not UTF-8 text and cannot be read.", ok=False)
            self._seen[rel] = _sha(data)
        lines = text.splitlines()
        total = len(lines)
        start = max(1, int(start_line or 1))
        window = (
            READ_DEFAULT_LINES if max_lines is None else max(1, min(int(max_lines), READ_MAX_LINES))
        )
        if total == 0:
            return ToolResult(f"{rel}: empty file (0 lines).")
        if start > total:
            return ToolResult(
                f"{rel} has {total} lines; start_line {start} is past the end.", ok=False
            )
        end = min(total, start + window - 1)
        column = max(1, int(start_column or 1))
        if column > max(1, len(lines[start - 1])):
            return ToolResult(
                f"Line {start} has {len(lines[start - 1])} chars; start_column {column} "
                "is past the end. Retry with start_column=1.",
                ok=False,
            )
        body = []
        for n in range(start, end + 1):
            line = lines[n - 1]
            offset = column - 1 if n == start else 0
            shown = line[offset : offset + READ_LINE_CHARS]
            body.append(f"{n:6d}\t{shown}")
            if offset + len(shown) < len(line):
                body[-1] += f" [line truncated: {len(line)} chars]"
                body.append(
                    f"[columns {offset + 1}-"
                    f"{offset + len(shown)} shown]. Continue with read_file(path={rel!r}, "
                    f"start_line={n}, max_lines=1, start_column={offset + len(shown) + 1})."
                )
        header = f"{rel}: lines {start}-{end} of {total}"
        out = [header, *body]
        if end < total:
            out.append(
                f"... {total - end} more line(s). Continue with read_file(path={rel!r}, "
                f"start_line={end + 1})."
            )
        return ToolResult("\n".join(out))

    # -- grep ---------------------------------------------------------------------------------

    def grep(
        self,
        pattern: str,
        regex: bool = False,
        path_glob: str | None = None,
        context: int = 0,
        max_results: int | None = None,
        offset: int = 0,
    ) -> ToolResult:
        """Search file contents with ripgrep; matches are ordered by path then line."""
        if not (pattern or "").strip():
            return ToolResult("Missing pattern. Give the text (or regex) to look for.", ok=False)
        cap = (
            GREP_DEFAULT_RESULTS
            if max_results is None
            else max(1, min(int(max_results), GREP_MAX_RESULTS))
        )
        offset = max(0, int(offset or 0))
        context = max(0, min(int(context or 0), GREP_MAX_CONTEXT))
        argv = ["rg", "--json", "--no-config", "--no-ignore", "--hidden", "--sort", "path"]
        argv += ["--max-filesize", str(READ_MAX_FILE_BYTES), "--max-columns", "2000"]
        argv += ["-F"] if not regex else []
        if path_glob:
            if path_glob.startswith("/") or ".." in Path(path_glob).parts:
                return ToolResult(
                    f"path_glob {path_glob!r} must be relative to the workspace, e.g. 'pkg/**'.",
                    ok=False,
                )
            argv += ["--glob", path_glob]
        argv += ["-e", pattern, "--", "."]
        label = f"{pattern!r}" + (f" in {path_glob!r}" if path_glob else "")
        stream = _run_bounded(argv, self.root, GREP_MAX_OUTPUT_BYTES, GREP_RG_TIMEOUT_S)
        if isinstance(stream, str):
            return ToolResult(f"grep failed to run: {stream}", ok=False)
        stdout, stderr, code, overflow = stream
        if overflow:
            return ToolResult(
                f"Too many matching lines for {label} (more than {GREP_MAX_OUTPUT_BYTES} bytes "
                "of results). Narrow the search with a longer pattern or a path_glob.",
                ok=False,
            )
        if code == 2:
            message = stderr.decode("utf-8", "replace").strip().splitlines()
            kind = "regex" if regex else "pattern"
            return ToolResult(
                f"Invalid {kind} {pattern!r}: {message[-1] if message else 'ripgrep error'}. "
                + ("Fix the regex or set regex=false for a literal search." if regex else ""),
                ok=False,
            )
        hits = self._parse_rg(stdout)
        if not hits:
            return ToolResult(f"No matches for {label}. Try a shorter or different pattern.")
        shown = hits[offset : offset + cap]
        if not shown:
            return ToolResult(
                f"{len(hits)} matching lines for {label}; offset {offset} is past the end.",
                ok=False,
            )
        header = (
            f"{len(hits)} matching lines for {label}; showing {offset + 1}-{offset + len(shown)}"
        )
        out = [header]
        cache: dict[str, list[str]] = {}
        for rel, number, text in shown:
            if context:
                lines = cache.setdefault(rel, self._lines_of(rel))
                for n in range(max(1, number - context), number):
                    out.append(f"{rel}-{n}-{_trim(lines[n - 1])}")
            out.append(f"{rel}:{number}: {_trim(text)}")
            if context:
                lines = cache.setdefault(rel, self._lines_of(rel))
                for n in range(number + 1, min(len(lines), number + context) + 1):
                    out.append(f"{rel}-{n}-{_trim(lines[n - 1])}")
        if offset + len(shown) < len(hits):
            out.append(
                f"... {len(hits) - offset - len(shown)} more. Continue with "
                f"grep(pattern={pattern!r}, offset={offset + len(shown)}) or narrow with "
                "path_glob."
            )
        return ToolResult("\n".join(out))

    def _parse_rg(self, stdout: bytes) -> list[tuple[str, int, str]]:
        hits: list[tuple[str, int, str]] = []
        for raw in stdout.splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event["data"]
            rel = data["path"].get("text")
            if rel is None:
                continue  # non-UTF-8 path
            rel = rel[2:] if rel.startswith("./") else rel
            target = self.root / rel
            if Path(os.path.realpath(target)) != target or not target.is_file():
                continue
            text = data["lines"].get("text", "").rstrip("\n")
            hits.append((rel, int(data["line_number"]), text))
        return hits

    def _lines_of(self, rel: str) -> list[str]:
        path = self.root / rel
        try:
            if path.stat().st_size > READ_MAX_FILE_BYTES:
                return []
            return path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []

    # -- editing --------------------------------------------------------------------------------

    def edit_file(self, path: str, old_text: str, new_text: str) -> ToolResult:
        resolved = self._resolve(path)
        if isinstance(resolved, ToolResult):
            return resolved
        rel = self._relative(resolved)
        if not isinstance(old_text, str) or not old_text:
            return ToolResult("old_text must be a non-empty string copied from the file.", ok=False)
        if not isinstance(new_text, str):
            return ToolResult("new_text must be a string (empty to delete old_text).", ok=False)
        data = self._read_bounded(resolved, rel)
        if isinstance(data, ToolResult):
            return data
        current = _sha(data)
        if rel not in self._seen:
            return ToolResult(
                f"Read {rel!r} with read_file before editing it, so the edit targets the current "
                "content.",
                ok=False,
            )
        if self._seen[rel] != current:
            return ToolResult(
                f"{rel!r} changed since you last read it (for example by run_tests or an earlier "
                "edit). Call read_file again and retry the edit against the current content.",
                ok=False,
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(f"File {rel!r} is not UTF-8 text and cannot be edited.", ok=False)
        count = text.count(old_text)
        if count == 0:
            return ToolResult(
                f"old_text not found in {rel!r}. Copy the exact text (including whitespace) from "
                "read_file output; do not include line numbers.",
                ok=False,
            )
        if count > 1:
            return ToolResult(
                f"old_text occurs {count} times in {rel!r}; the edit must be unique. Include "
                "more surrounding lines so exactly one place matches.",
                ok=False,
            )
        updated = text.replace(old_text, new_text, 1)
        self._atomic_write(resolved, updated.encode("utf-8"))
        self._seen[rel] = _sha(updated.encode("utf-8"))
        diff = _unified(rel, text, updated)
        return ToolResult(f"Edited {rel}.\n{diff}")

    def write_file(self, path: str, content: str) -> ToolResult:
        resolved = self._resolve(path, must_exist=False)
        if isinstance(resolved, ToolResult):
            return resolved
        if not isinstance(content, str):
            return ToolResult("content must be a string.", ok=False)
        rel = resolved.relative_to(self.root).as_posix()
        if resolved.exists():
            return ToolResult(
                f"{rel!r} already exists. write_file creates new files only; use edit_file to "
                "change an existing file.",
                ok=False,
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(resolved, content.encode("utf-8"))
        self._seen[rel] = _sha(content.encode("utf-8"))
        self._created.add(rel)
        return ToolResult(f"Created {rel} ({len(content.splitlines())} lines).")

    def remove_file(self, path: str) -> ToolResult:
        """Remove an agent-created file only after observing its current contents."""
        resolved = self._resolve(path)
        if isinstance(resolved, ToolResult):
            return resolved
        rel = self._relative(resolved)
        if rel not in self._created or (self.snapshot / rel).exists():
            return ToolResult(
                "remove_file only removes files created with write_file in this workspace; "
                "pre-existing files are protected.",
                ok=False,
            )
        data = self._read_bounded(resolved, rel)
        if isinstance(data, ToolResult):
            return data
        if self._seen.get(rel) != _sha(data):
            return ToolResult(
                f"{rel!r} changed since your last observation. Read it with read_file "
                "and confirm it is still disposable before removing it.",
                ok=False,
            )
        resolved.unlink()
        self._created.remove(rel)
        self._seen.pop(rel, None)
        return ToolResult(f"Removed {rel}. Workspace file deleted; conversation unchanged.")

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        """Write to a temp file in the same directory, fsync, then replace in one step.

        An existing file keeps its permission bits, so an edited shell script stays executable.
        """
        fd, tmp = tempfile.mkstemp(prefix=".fa-edit-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                os.chmod(tmp, os.lstat(target).st_mode & 0o777)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def changes(self) -> list[Change]:
        """Typed manifest of every entry that differs from the snapshot, by sha256 and kind.

        Regular files are compared by hash (so same-size binary changes count); symlinks,
        FIFOs, sockets and devices that appear in the live tree are reported as `unsupported`
        instead of being hidden.
        """
        before = inspect_tree(self.snapshot, self.max_entries)
        after = inspect_tree(self.root, self.max_entries)
        found: list[Change] = []
        for rel in sorted(set(before.files) | set(after.files)):
            old: FileEntry | None = before.files.get(rel)
            new: FileEntry | None = after.files.get(rel)
            if old is not None and new is not None and old.sha256 == new.sha256:
                continue
            if old is None:
                status = "added"
            elif new is None:
                status = "deleted"
            else:
                status = "modified" if _is_text(self.root / rel) else "binary_modified"
            found.append(
                Change(
                    rel,
                    status,
                    "file",
                    before_sha=old.sha256 if old else None,
                    after_sha=new.sha256 if new else None,
                    before_size=old.size if old else None,
                    after_size=new.size if new else None,
                )
            )
        specials = [(rel, "symlink") for rel in after.symlinks] + list(after.unsupported)
        for rel, kind in sorted(specials):
            found.append(Change(rel, "unsupported", kind))
        if after.truncated:
            found.append(Change("", "unsupported", "unknown"))
        return sorted(found, key=lambda c: c.path)

    def diff(self) -> ToolResult:
        """Header line per change (status, path, sizes/hashes) plus unified diffs for text."""
        chunks: list[str] = []
        for change in self.changes():
            if change.status == "unsupported":
                if change.kind == "unknown" and not change.path:
                    chunks.append(f"[manifest truncated at {self.max_entries} entries]")
                else:
                    chunks.append(
                        f"unsupported {change.path} ({change.kind}): not a regular file, "
                        "ignored by every tool"
                    )
                continue
            sizes = f"{change.before_size} -> {change.after_size} bytes"
            hashes = f"{(change.before_sha or '-')[:12]} -> {(change.after_sha or '-')[:12]}"
            chunks.append(f"{change.status} {change.path} ({sizes}, sha256 {hashes})")
            if change.status == "binary_modified":
                continue
            before = _text_or_none(self.snapshot / change.path) if change.before_sha else None
            after = _text_or_none(self.root / change.path) if change.after_sha else None
            if before is None and after is None:
                continue
            chunks.append(
                _unified(
                    change.path,
                    before or "",
                    after or "",
                    missing=(change.before_sha is None, change.after_sha is None),
                )
            )
        if not chunks:
            return ToolResult("No changes relative to the starting snapshot.")
        text = "\n".join(chunks)
        if len(text) > DIFF_MAX_CHARS:
            text = text[:DIFF_MAX_CHARS] + f"\n[diff truncated at {DIFF_MAX_CHARS} chars]"
        return ToolResult(text)

    # -- tests --------------------------------------------------------------------------------

    def run_tests(
        self, tests: Any, timeout_s: float = 60.0, *, diagnostic: bool = False
    ) -> ToolResult:
        """A validated selection, executed only inside the sandbox. No shell, no options."""
        if type(diagnostic) is not bool:
            return ToolResult("diagnostic must be true or false.", ok=False)
        if diagnostic and self.tests.kind != "pytest":
            return ToolResult("diagnostic mode is available for pytest tasks only.", ok=False)
        if self.sandbox is None:
            return ToolResult(
                "run_tests is blocked: no verified sandbox is available in this run.", ok=False
            )
        if isinstance(tests, str):
            tests = [tests]
        if not isinstance(tests, list) or not tests:
            return ToolResult(
                f"tests must be a non-empty list: {self.tests.describe_selection()}.", ok=False
            )
        if self.tests.kind == "shell":
            return self._run_shell_tests(tests, timeout_s)
        if len(tests) > MAX_TEST_IDS:
            return ToolResult(f"At most {MAX_TEST_IDS} test ids per call.", ok=False)
        ids: list[str] = []
        for item in tests:
            if not isinstance(item, str) or not NODE_ID_RE.match(item):
                return ToolResult(
                    f"Invalid test id {item!r}. Give a relative path or pytest node id only "
                    "(no options, no spaces, no shell syntax).",
                    ok=False,
                )
            file_part = item.split("::", 1)[0]
            resolved = self._resolve(file_part, must_exist=False)
            if isinstance(resolved, ToolResult):
                return resolved
            if not resolved.exists():
                return ToolResult(
                    f"Test path {file_part!r} does not exist in the workspace. Use a path from "
                    "list_files.",
                    ok=False,
                )
            ids.append(item)
        # Pytest dumps thread stacks before our outer wall-clock timeout kills the process.
        dump_after = min(10.0, max(0.1, timeout_s / 2))
        argv = [
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--color=no",
            "-o",
            f"faulthandler_timeout={dump_after}",
            *(["-vv", "-s", "--tb=short"] if diagnostic else ["-q"]),
            "--",
            *ids,
        ]
        try:
            outcome = self.sandbox.run_python(self.root, argv, timeout_s=timeout_s)
        except SandboxUnavailable as error:
            return ToolResult(f"run_tests is blocked: {error}", ok=False)
        return self._report_run(
            f"pytest {' '.join(ids)}", outcome, timeout_s, _pytest_meaning(outcome.exit_code)
        )

    def _run_shell_tests(self, tests: list, timeout_s: float) -> ToolResult:
        """Each selected script runs as `bash <path>` in its own sandbox run, in order."""
        if len(tests) > MAX_SHELL_SCRIPTS:
            return ToolResult(
                f"At most {MAX_SHELL_SCRIPTS} scripts per call; use {self.tests.suite!r} for "
                "the whole suite."
                if self.tests.suite
                else f"At most {MAX_SHELL_SCRIPTS} scripts per call.",
                ok=False,
            )
        paths: list[str] = []
        for item in tests:
            if not isinstance(item, str) or not NODE_ID_RE.match(item) or "::" in item:
                return ToolResult(
                    f"Invalid test selection {item!r}. Give {self.tests.describe_selection()} "
                    "(no options, no spaces, no shell syntax).",
                    ok=False,
                )
            if not self.tests.selects(item):
                return ToolResult(
                    f"{item!r} is not a test script of this task. Give "
                    f"{self.tests.describe_selection()}.",
                    ok=False,
                )
            resolved = self._resolve(item)
            if isinstance(resolved, ToolResult):
                return resolved
            paths.append(self._relative(resolved))
        results: list[ToolResult] = []
        for path in paths:
            try:
                outcome = self.sandbox.run(
                    self.root, [SHELL_INTERPRETER, path], timeout_s=timeout_s
                )
            except SandboxUnavailable as error:
                return ToolResult(f"run_tests is blocked: {error}", ok=False)
            meaning = "script passed" if outcome.exit_code == 0 else "script failed"
            results.append(
                self._report_run(f"{SHELL_INTERPRETER} {path}", outcome, timeout_s, meaning)
            )
            if not results[-1].ok:
                break
        return ToolResult("\n\n".join(r.text for r in results), ok=all(r.ok for r in results))

    def _report_run(self, title: str, outcome: Any, timeout_s: float, meaning: str) -> ToolResult:
        parts = [title]
        if outcome.timed_out:
            parts.append(f"TIMED OUT after {timeout_s}s; the process tree was killed.")
        elif outcome.error:
            parts.append(f"The run did not complete: {outcome.error}. Output below is partial.")
        elif outcome.killed:
            parts.append(f"KILLED: {outcome.killed}.")
        else:
            parts.append(f"exit code {outcome.exit_code} ({meaning})")
        changes = outcome.changes
        if changes is not None and changes.applied and (changes.written or changes.deleted):
            parts.append(
                "Files changed by the run were imported into the workspace: "
                + ", ".join([*changes.written, *(f"{d} (deleted)" for d in changes.deleted)])
                + ". Re-read a file before editing it."
            )
        elif changes is not None and not changes.applied and outcome.completed:
            parts.append(f"Files changed by the run were not imported: {changes.error}.")
        if outcome.truncated:
            parts.append("Capture limit reached: the saved output is partial, not the full log.")
        body = outcome.stdout
        if outcome.stderr.strip():
            body += "\n--- stderr ---\n" + outcome.stderr
        output_id = f"test-output:{len(self._test_outputs) + 1}"
        captured = "\n".join([*parts, body.rstrip()])
        self._test_outputs[output_id] = captured
        output_dir = self.root.parent / "test-output"
        output_dir.mkdir(exist_ok=True)
        # Outside the candidate tree: reading an artifact cannot change the project diff.
        with (output_dir / f"{len(self._test_outputs)}.txt").open("x", encoding="utf-8") as handle:
            handle.write(captured)
        parts.append(
            f"Captured output: read_file(path={output_id!r}); use returned line/column "
            "continuations. This artifact is read-only and survives later test calls."
        )
        if len(body) > TEST_OUTPUT_MAX_CHARS:
            body = (
                body[:TEST_OUTPUT_MAX_CHARS]
                + f"\n[output truncated at {TEST_OUTPUT_MAX_CHARS} chars]"
            )
        parts.append(body.rstrip())
        return ToolResult("\n".join(parts), ok=outcome.completed)

    # -- dispatch ---------------------------------------------------------------------------

    names = CODING_TOOL_NAMES

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        if self.tests.kind == "pytest":
            return CODING_TOOL_SCHEMAS
        return {**CODING_TOOL_SCHEMAS, "run_tests": _shell_run_tests_schema(self.tests)}

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        a = arguments
        try:
            if name == "list_files":
                return self.list_files()
            if name == "read_file":
                return self.read_file(
                    str(a.get("path", "")),
                    _int(a.get("start_line"), 1),
                    _opt_int(a.get("max_lines")),
                    _int(a.get("start_column"), 1),
                )
            if name == "grep":
                return self.grep(
                    str(a.get("pattern", "")),
                    regex=bool(a.get("regex", False)),
                    path_glob=a.get("path_glob") or None,
                    context=_int(a.get("context"), 0),
                    max_results=_opt_int(a.get("max_results")),
                    offset=_int(a.get("offset"), 0),
                )
            if name == "edit_file":
                return self.edit_file(str(a.get("path", "")), a.get("old_text"), a.get("new_text"))
            if name == "write_file":
                return self.write_file(str(a.get("path", "")), a.get("content"))
            if name == "remove_file":
                return self.remove_file(str(a.get("path", "")))
            if name == "diff":
                return self.diff()
            if name == "run_tests":
                return self.run_tests(a.get("tests"), diagnostic=a.get("diagnostic", False))
        except (TypeError, ValueError, OverflowError) as error:
            return ToolResult(
                f"Invalid arguments for {name}: {error}. Correct them and retry.", ok=False
            )
        except OSError as error:
            return ToolResult(
                f"Filesystem operation {name} failed: {error}. Inspect the current file state "
                "before retrying; an operation may have partially completed.",
                ok=False,
            )
        return ToolResult(f"Unknown tool {name!r}.", ok=False)


def _int(value: Any, default: int) -> int:
    return default if value is None else int(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _trim(text: str) -> str:
    return text if len(text) <= GREP_LINE_CHARS else text[: GREP_LINE_CHARS - 1] + "…"


def _run_bounded(
    argv: list[str], cwd: Path, max_bytes: int, timeout_s: float
) -> tuple[bytes, bytes, int, bool] | str:
    """Run a helper, reading at most `max_bytes` of stdout; kill it when it writes more."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        return f"{type(error).__name__}: {error}"
    try:
        stdout = proc.stdout.read(max_bytes + 1)
        overflow = len(stdout) > max_bytes
        if overflow:
            proc.kill()
        stderr = proc.stderr.read(64_000)
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        return f"timed out after {timeout_s}s"
    finally:
        proc.stdout.close()
        proc.stderr.close()
    return stdout, stderr, proc.returncode, overflow


def _is_text(path: Path) -> bool:
    try:
        if path.stat().st_size > DIFF_MAX_FILE_BYTES:
            return False
        path.read_bytes().decode("utf-8")
        return True
    except (OSError, UnicodeDecodeError):
        return False


def _text_or_none(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if path.stat().st_size > DIFF_MAX_FILE_BYTES:
            return f"<{path.stat().st_size} bytes, too large to diff>\n"
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"<binary {path.stat().st_size} bytes>\n"


def _unified(rel: str, before: str, after: str, missing: tuple[bool, bool] = (False, False)) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="/dev/null" if missing[0] else f"a/{rel}",
        tofile="/dev/null" if missing[1] else f"b/{rel}",
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    ).rstrip("\n")


def _pytest_meaning(code: int | None) -> str:
    return {
        0: "all selected tests passed",
        1: "some tests failed",
        2: "pytest was interrupted or usage error",
        3: "internal pytest error",
        4: "pytest usage error",
        5: "no tests were collected",
    }.get(code, "unknown")


# -- model-facing schemas ----------------------------------------------------------------------

CODING_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "description": load_tool_description("list_files"),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "read_file": {
        "description": load_tool_description("read_file"),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative workspace path or exact test-output: ID "
                    "returned by run_tests.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to show, 1-based. Default 1; use the continuation "
                    "value returned by read_file or a line number from grep.",
                },
                "start_column": {
                    "type": "integer",
                    "description": "First character of start_line, 1-based; default 1. "
                    "For a truncated line, copy the returned start_column with start_line "
                    "and max_lines=1. Counts Unicode characters, not bytes.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": f"Lines to show, 1-{READ_MAX_LINES}. "
                    f"Default {READ_DEFAULT_LINES}.",
                },
            },
            "required": ["path"],
        },
    },
    "grep": {
        "description": load_tool_description("grep"),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text to find. Literal unless regex is true.",
                },
                "regex": {
                    "type": "boolean",
                    "description": "Treat pattern as a regular expression. Default false.",
                },
                "path_glob": {
                    "type": "string",
                    "description": "Restrict to a glob based on paths from list_files, e.g. "
                    "'src/**/*.py'. Omit to search all accessible files.",
                },
                "context": {
                    "type": "integer",
                    "description": f"Lines of context around each match, 0-{GREP_MAX_CONTEXT}. "
                    "Default 0.",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Matches per call, 1-{GREP_MAX_RESULTS}. "
                    f"Default {GREP_DEFAULT_RESULTS}.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip this many matches (continuation). Default 0.",
                },
            },
            "required": ["pattern"],
        },
    },
    "edit_file": {
        "description": load_tool_description("edit_file"),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative workspace path; captured test-output: "
                    "logs are read-only.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to replace, copied from read_file output "
                    "without line numbers.",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text; empty string deletes old_text.",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    "write_file": {
        "description": load_tool_description("write_file"),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "New path relative to the workspace, e.g. "
                    "'tests/test_theme.py'. "
                    "Choose from the task layout; no absolute paths, .., or symlinks.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete UTF-8 source text for the new file, "
                    "including needed newlines.",
                },
            },
            "required": ["path", "content"],
        },
    },
    "remove_file": {
        "description": load_tool_description("remove_file"),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Exact relative path of a disposable file "
                    "you created with write_file.",
                }
            },
            "required": ["path"],
        },
    },
    "diff": {
        "description": load_tool_description("diff"),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "run_tests": {
        "description": load_tool_description("run_tests"),
        "parameters": {
            "type": "object",
            "properties": {
                "diagnostic": {
                    "type": "boolean",
                    "description": "Default false. Set true for a focused failing/hanging test: "
                    "show test names and uncaptured output, including successful-test prints.",
                },
                "tests": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Select 1-20 relative test paths or pytest "
                    "node ids from list_files "
                    "and read_file, e.g. ['tests'] or ['tests/test_x.py::test_name']; "
                    "no options or shell syntax.",
                },
            },
            "required": ["tests"],
        },
    },
}


def _shell_run_tests_schema(spec: TestSpec) -> dict[str, Any]:
    """run_tests schema for shell tasks; pytest diagnostic mode is not offered."""
    return {
        "description": "Run selected project shell test scripts "
        "in an isolated, network-free sandbox.\n"
        "- Use a focused selection to reproduce a failure or verify a fix; inspect exit status.\n"
        "- Use read_file with the returned test-output: ID to inspect captured logs.\n"
        "- Execution timeout kills the process tree. Each call starts fresh: /tmp and process "
        "state do not persist. Re-read workspace files reported as changed before editing.",
        "parameters": {
            "type": "object",
            "properties": {
                "tests": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Select 1-{MAX_SHELL_SCRIPTS} workspace paths: "
                    f"{spec.describe_selection()}; no options or shell syntax. "
                    "Scripts run sequentially; an incomplete execution stops the batch.",
                }
            },
            "required": ["tests"],
        },
    }


__all__ = ["CODING_TOOL_NAMES", "CODING_TOOL_SCHEMAS", "Change", "TestSpec", "Workspace"]
