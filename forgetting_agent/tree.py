"""Bounded, lstat-based inspection and copying of directory trees.

Every place the harness touches a tree it may not trust (a task snapshot, the live workspace
after a sandbox run) goes through here. Only directories and regular files are supported;
symlinks are skipped and counted, FIFOs, sockets and devices are reported as unsupported so
callers can refuse the tree before anything opens them (opening a FIFO blocks forever). Entry
counts are bounded; nothing here follows a symlink or reads a file that is not a regular file.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DEFAULT_MAX_ENTRIES = 5_000
HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class FileEntry:
    size: int
    sha256: str


@dataclass(frozen=True)
class TreeReport:
    """What a bounded lstat walk found: regular files (hashed), skipped and refused entries."""

    files: dict[str, FileEntry] = field(default_factory=dict)  # relative posix path -> entry
    bytes: int = 0
    symlinks: tuple[str, ...] = ()
    unsupported: tuple[tuple[str, str], ...] = ()  # (path, kind) for fifo/socket/device/unknown
    unreadable: tuple[str, ...] = ()
    truncated: bool = False  # the entry bound was hit; `files` is incomplete

    @property
    def ok(self) -> bool:
        return not (self.unsupported or self.unreadable or self.truncated)

    def problems(self) -> str:
        parts = []
        if self.unsupported:
            parts.append(
                "unsupported entries: " + ", ".join(f"{p} ({k})" for p, k in self.unsupported[:5])
            )
        if self.unreadable:
            parts.append("unreadable entries: " + ", ".join(self.unreadable[:5]))
        if self.truncated:
            parts.append("too many entries")
        return "; ".join(parts)


def kind_of(mode: int) -> str:
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
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "unknown"


def sha256_of(path: Path, limit: int | None = None) -> str:
    """Hash a regular file; `limit` caps the bytes read (callers check sizes first)."""
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK if remaining is None else min(HASH_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    return digest.hexdigest()


def inspect_tree(
    root: Path, max_entries: int = DEFAULT_MAX_ENTRIES, hashes: bool = True
) -> TreeReport:
    """lstat walk of `root` in sorted order, stopping at `max_entries` entries (files + dirs)."""
    root = Path(root)
    files: dict[str, FileEntry] = {}
    total = 0
    symlinks: list[str] = []
    unsupported: list[tuple[str, str]] = []
    unreadable: list[str] = []
    seen = 0
    truncated = False
    stack = [root]
    while stack and not truncated:
        directory = stack.pop()
        try:
            names = sorted(os.listdir(directory), reverse=True)
        except OSError:
            unreadable.append(_rel(root, directory))
            continue
        children: list[Path] = []
        for name in names:
            path = directory / name
            seen += 1
            if seen > max_entries:
                truncated = True
                break
            rel = _rel(root, path)
            try:
                st = os.lstat(path)
            except OSError:
                unreadable.append(rel)
                continue
            kind = kind_of(st.st_mode)
            if kind == "dir":
                children.append(path)
            elif kind == "file":
                try:
                    digest = sha256_of(path) if hashes else ""
                except OSError:
                    unreadable.append(rel)
                    continue
                files[rel] = FileEntry(st.st_size, digest)
                total += st.st_size
            elif kind == "symlink":
                symlinks.append(rel)
            else:
                unsupported.append((rel, kind))
        stack.extend(children)  # reverse-sorted names: pops come out in sorted order
    return TreeReport(
        files=dict(sorted(files.items())),
        bytes=total,
        symlinks=tuple(sorted(symlinks)),
        unsupported=tuple(sorted(unsupported)),
        unreadable=tuple(sorted(unreadable)),
        truncated=truncated,
    )


def copy_tree(source: Path, target: Path, max_entries: int = DEFAULT_MAX_ENTRIES) -> TreeReport:
    """Copy directories and regular files (with their permission bits) into a new `target`.

    Refuses (raises ValueError, creates nothing) when the source has unsupported or unreadable
    entries or exceeds the entry bound. Symlinks are skipped; the report lists them.
    """
    report = inspect_tree(source, max_entries)
    if not report.ok:
        raise ValueError(f"refusing to copy {source}: {report.problems()}")
    target = Path(target)
    target.mkdir(parents=True, exist_ok=False)
    for rel in report.files:
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source) / rel, destination, follow_symlinks=False)
        shutil.copymode(Path(source) / rel, destination, follow_symlinks=False)
    return report


def safe_relative(path: object) -> str | None:
    """A relative posix path with plain components, or None when it cannot be trusted."""
    if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path:
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or "\\" in path:
        return None
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return pure.as_posix()


def has_symlink_component(root: Path, rel: str) -> bool:
    """True when any existing component of `root/rel` (root excluded) is a symlink."""
    current = Path(root)
    for part in PurePosixPath(rel).parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            return False
    return False


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "FileEntry",
    "TreeReport",
    "copy_tree",
    "has_symlink_component",
    "inspect_tree",
    "kind_of",
    "safe_relative",
    "sha256_of",
]
