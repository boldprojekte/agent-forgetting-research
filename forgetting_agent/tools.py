"""Task tools over a local text corpus and the model-facing tool schemas.

Task tools (`list_files`, `read_file`, `search_corpus`, `submit_answer`) are shared by every
experimental arm. Context-management tools (`context_apply`,
`context_recover`) are rendered only for the arm that has them; their behaviour lives in
context.py. All schemas are rendered from the single `TOOL_SCHEMAS` source below.

Every tool returns a `ToolResult`; failures are ordinary results with an instruction, never
exceptions crossing the loop boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import ToolImage


def load_tool_description(name: str) -> str:
    """Load the description sent in the native provider tool schema."""
    return (Path(__file__).parent / "prompts" / "tools" / f"{name}.md").read_text().strip()


CORPUS_TOOL_NAMES = ("list_files", "read_file", "search_corpus")
TASK_TOOL_NAMES = (*CORPUS_TOOL_NAMES, "submit_answer")
CONTEXT_TOOL_NAMES = ("context_apply", "context_recover")

SEARCH_CORPUS_MAX_HITS = 40


@dataclass(frozen=True)
class ToolResult:
    """Text and optional immutable images; `ok=False` marks a tool failure."""

    text: str
    ok: bool = True
    images: tuple[ToolImage, ...] = ()


class Corpus:
    """Read-only access to text files below one root directory.

    Path safety: a requested path must be relative, must not contain `..`, and must resolve
    (following symlinks) to a regular file inside the resolved root. Symlinks that point
    outside the root are rejected, and listing/search skip them as well.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"corpus root is not a directory: {self.root}")

    # -- listing --------------------------------------------------------------------------

    def _safe_files(self) -> list[Path]:
        """All regular files whose resolved location stays inside the root, sorted."""
        files: list[Path] = []
        for candidate in sorted(self.root.rglob("*")):
            if candidate.is_file() and self._inside_root(candidate.resolve()):
                files.append(candidate)
        return files

    def _inside_root(self, resolved: Path) -> bool:
        return resolved == self.root or self.root in resolved.parents

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def list_files(self) -> ToolResult:
        names = [self._relative(p) for p in self._safe_files()]
        if not names:
            return ToolResult("Corpus is empty.", ok=True)
        return ToolResult("\n".join(names))

    # -- reading ----------------------------------------------------------------------------

    def _resolve(self, path: str) -> Path | ToolResult:
        """Resolve a model-supplied path or return the error result explaining the fix."""
        cleaned = (path or "").strip()
        if not cleaned:
            return ToolResult("Missing path. Call list_files to see corpus paths.", ok=False)
        if Path(cleaned).is_absolute() or ".." in Path(cleaned).parts:
            return ToolResult(
                f"Path {cleaned!r} is not a relative path inside the corpus. "
                "Use a path exactly as listed by list_files.",
                ok=False,
            )
        candidate = self.root / cleaned
        if not candidate.exists():
            return ToolResult(
                f"No file {cleaned!r} in the corpus. Call list_files to see valid paths.",
                ok=False,
            )
        resolved = candidate.resolve()
        if not self._inside_root(resolved) or not resolved.is_file():
            return ToolResult(
                f"Path {cleaned!r} does not resolve to a regular file inside the corpus "
                "and cannot be read.",
                ok=False,
            )
        return resolved

    def read_file(self, path: str) -> ToolResult:
        resolved = self._resolve(path)
        if isinstance(resolved, ToolResult):
            return resolved
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(f"File {path!r} is not UTF-8 text and cannot be read.", ok=False)
        return ToolResult(text)

    # -- searching ----------------------------------------------------------------------------

    def search_corpus(self, query: str) -> ToolResult:
        """Case-insensitive substring search over lines of every safe file."""
        needle = (query or "").strip().lower()
        if not needle:
            return ToolResult("Missing query. Give a word or phrase to look for.", ok=False)
        hits: list[str] = []
        for file in self._safe_files():
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(lines, start=1):
                if needle in line.lower():
                    hits.append(f"{self._relative(file)}:{number}: {line.strip()}")
        if not hits:
            return ToolResult(f"No lines match {query!r}. Try a shorter or different term.")
        shown = hits[:SEARCH_CORPUS_MAX_HITS]
        header = f"{len(hits)} matching line(s) for {query!r}"
        if len(hits) > len(shown):
            header += f"; showing first {len(shown)}. Narrow the query to see the rest."
        return ToolResult("\n".join([header, *shown]))

    # -- TaskTools protocol (see loop.py) ---------------------------------------------------

    names = CORPUS_TOOL_NAMES

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        return {name: TOOL_SCHEMAS[name] for name in CORPUS_TOOL_NAMES}

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "list_files":
            return self.list_files()
        if name == "read_file":
            return self.read_file(str(arguments.get("path", "")))
        if name == "search_corpus":
            return self.search_corpus(str(arguments.get("query", "")))
        return ToolResult(f"Unknown tool {name!r}.", ok=False)


# -- model-facing schemas -----------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "description": "List corpus file paths.\n"
        "- Call when you do not know a path or a path lookup failed.\n"
        "- Use search_corpus for a known content term; use read_file for a listed file.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "read_file": {
        "description": "Read the complete text of one corpus file.\n"
        "- Use when a listed path or search hit contains evidence needed for your answer.\n"
        "- Use search_corpus to locate terms, or list_files when the path is unknown.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path exactly as listed by list_files, "
                    "e.g. 'site-survey.md'.",
                }
            },
            "required": ["path"],
        },
    },
    "search_corpus": {
        "description": "Find corpus lines containing a case-insensitive substring.\n"
        "- Use when you know a content term but not its source file.\n"
        "- Use read_file for the surrounding evidence; use list_files for paths, not content.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Word or short phrase to look for, e.g. 'flood level'.",
                }
            },
            "required": ["query"],
        },
    },
    "submit_answer": {
        "description": "Submit the answer and end work on the current user request.\n"
        "- Call once per completed user request, after the requested work and required "
        "verification. Recheck the original request and working plan first.\n"
        "- Pending, partial, unverified or failed required work means the request is not "
        "complete unless an external blocker prevents further progress.\n"
        "- Context size, token usage, elapsed work or a desire to wrap up are not blockers "
        "and are never reasons to submit. Use available context-management tools and "
        "continue.\n"
        "- A later user request needs a new submission, even if history "
        "contains earlier submissions.\n"
        "- Must be the only tool call in its message.\n"
        "- Use read_file if evidence is still missing; do not submit a plan as completed work.",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "Complete answer for the user in the "
                    "requested format; distinguish "
                    "verified results from blockers and unverified claims.",
                }
            },
            "required": ["answer"],
        },
    },
    "context_apply": {
        "description": load_tool_description("context_apply"),
        "parameters": {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "description": "Non-empty list, one entry per result from a "
                    "visible context_result envelope. "
                    "Select each result once; the whole batch is atomic.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Stable result ID copied from that output's "
                                "context_result envelope, e.g. 'r42'. Never invent an ID. "
                                "IDs reset per chat and are never reused within it.",
                            },
                            "note": {
                                "type": "string",
                                "description": "Write a sufficient note for "
                                "continued work: retain relevant "
                                "facts, decisions, constraints "
                                "and source locations, or explain why "
                                "nothing is needed. Use as much "
                                "detail as necessary, not a sentence quota.",
                            },
                        },
                        "required": ["id", "note"],
                    },
                }
            },
            "required": ["targets"],
        },
    },
    "context_recover": {
        "description": load_tool_description("context_recover"),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Exact archive reference copied from a stub or Apply "
                    "receipt, e.g. 'ic-0001'. "
                    "Result IDs such as 'r42' are not recovery references.",
                }
            },
            "required": ["ref"],
        },
    },
}


def render_tools(
    names: tuple[str, ...], schemas: dict[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Render OpenAI function-tool entries for the given tool names, in the given order."""
    lookup = TOOL_SCHEMAS if schemas is None else schemas
    rendered = []
    for name in names:
        schema = lookup[name]
        rendered.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                },
            }
        )
    return rendered


def parse_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any] | ToolResult:
    """Parse tool-call arguments leniently: JSON text, an already-parsed dict, or empty."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Tolerate leaked markup such as ```json fences around otherwise valid JSON.
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as error:
            return ToolResult(
                f"Arguments are not valid JSON: {error.msg} at line {error.lineno}, "
                f"column {error.colno} (character {error.pos}). This call was not executed. "
                "Correct the arguments and retry with a JSON object using the documented fields.",
                ok=False,
            )
    if not isinstance(parsed, dict):
        return ToolResult("Arguments must be a JSON object, not a list or scalar.", ok=False)
    return parsed


def format_call(name: str, arguments: dict[str, Any], limit: int = 80) -> str:
    """Compact `tool(args)` rendering used in stubs and diagnostics."""
    body = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    if len(body) > limit:
        body = body[: limit - 1] + "…"
    return f"{name}({body})"
