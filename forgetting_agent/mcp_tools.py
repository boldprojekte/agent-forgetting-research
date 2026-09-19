"""Eager MCP tool discovery composed with the existing synchronous TaskTools boundary.

Hosts supply trusted, unentered SDK Clients (stdio, Streamable HTTP or in-process).
One AnyIO portal owns their async lifetimes for the entire episode. No tool search,
server-prompt injection, implicit resource reads, tool retry or background execution.
"""

from __future__ import annotations

import json
import math
import re
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from functools import partial
from importlib.metadata import version
from typing import Any

import anyio
from anyio.from_thread import BlockingPortal, start_blocking_portal
from jsonschema import validators
from mcp import Client

from .loop import TaskTools
from .media import ToolImage
from .tools import CONTEXT_TOOL_NAMES, ToolResult
from .trace import Trace


def _public_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove protocol metadata, never user-data keys inside structuredContent."""
    result = deepcopy(blocks)
    for block in result:
        block.pop("_meta", None)
        if block.get("type") == "resource":
            block["resource"].pop("_meta", None)
    return result


def _render(raw: dict[str, Any]) -> ToolResult:
    blocks = raw.get("content", [])
    unsupported = [
        b.get("type")
        for b in blocks
        if b.get("type") not in ("text", "image", "resource_link", "resource")
        or (b.get("type") == "resource" and "text" not in b.get("resource", {}))
    ]
    if unsupported:
        return ToolResult(
            "MCP returned unsupported media (only text and images are supported): "
            f"{', '.join(unsupported)}. The full response is preserved in the audit trace, "
            "but was not shown. Request a text result if the tool supports it. "
            "The call already ran; do not repeat side effects.",
            ok=False,
        )
    images = []
    if any(b.get("type") == "image" for b in blocks):
        blocks = deepcopy(blocks)
        try:
            for index, block in enumerate(blocks):
                if block.get("type") == "image":
                    image = ToolImage(data=block["data"], mime_type=block["mimeType"])
                    images.append(image)
                    blocks[index] = {
                        "type": "text",
                        "text": f"[Attached image {len(images)}: {image.mime_type}]",
                        **({"annotations": block["annotations"]} if "annotations" in block else {}),
                    }
        except (ValueError, TypeError) as error:
            return ToolResult(
                f"Invalid MCP image: {error}. No image was displayed. "
                "Request a supported, valid image payload; the call already ran.",
                ok=False,
            )
    # Preserve text exactly for the common case; retain structured-only/different payloads.
    structured = raw.get("structuredContent")
    text = (
        blocks[0]["text"]
        if len(blocks) == 1
        and blocks[0].get("type") == "text"
        and set(blocks[0]) <= {"type", "text", "_meta"}
        else None
    )
    duplicate = False
    if text is not None and structured is not None:
        try:
            duplicate = json.loads(text) == structured
        except (ValueError, TypeError):
            pass
    if text is not None and (structured is None or duplicate):
        rendered = text
    else:
        rendered = json.dumps(
            {"content": _public_blocks(blocks), "structuredContent": structured}, ensure_ascii=False
        )
    return ToolResult(rendered, ok=not raw.get("isError", False), images=tuple(images))


class MCPTools:
    """Fixed tool surface for one episode. Construct with open_mcp_tools."""

    def __init__(
        self,
        base: TaskTools,
        clients: dict[str, Client],
        portal: BlockingPortal,
        trace: Trace,
        timeout: float,
    ):
        self._base, self._portal, self._trace, self._timeout = base, portal, trace, timeout
        self._routes: dict[str, tuple[Client, str, Any]] = {}
        self.schemas = deepcopy(base.schemas)
        names = list(base.names)
        reserved = set(names) | set(CONTEXT_TOOL_NAMES) | {"submit_answer"}
        for namespace, client in clients.items():
            seen_cursors: set[str] = set()
            cursor = None
            discovered = []
            while True:
                page = portal.call(
                    partial(self._bounded, client.list_tools, cursor=cursor, cache_mode="bypass")
                )
                discovered.extend(page.tools)
                cursor = page.next_cursor
                if cursor is None:
                    break
                if cursor in seen_cursors:
                    raise ValueError(f"MCP {namespace}: repeated tools/list cursor")
                seen_cursors.add(cursor)
            for tool in sorted(discovered, key=lambda t: t.name):
                name = f"mcp_{namespace}__{tool.name}"
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                    raise ValueError(f"MCP tool name cannot be represented by provider: {name!r}")
                if name in reserved:
                    raise ValueError(f"Duplicate tool name: {name}")
                reserved.add(name)
                schema = deepcopy(tool.input_schema)
                validator = validators.validator_for(schema)
                validator.check_schema(schema)
                self._routes[name] = client, tool.name, validator(schema)
                self.schemas[name] = {
                    "description": tool.description or f"Call {tool.name} on {namespace}.",
                    "parameters": schema,
                }
                names.append(name)
            trace.event(
                "mcp_catalog",
                namespace=namespace,
                sdk_version=version("mcp"),
                protocol_version=client.protocol_version,
                server_info=client.server_info.model_dump(mode="json", by_alias=True)
                if client.server_info
                else None,
                instructions=client.instructions,
                tools=[t.model_dump(mode="json", by_alias=True) for t in discovered],
            )
        self.names = tuple(names)

    async def _bounded(self, operation, *args, **kwargs):
        with anyio.fail_after(self._timeout):
            return await operation(*args, **kwargs)

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name in self._base.names:
            return self._base.call(name, arguments)
        if name not in self._routes:
            return ToolResult(f"Unknown MCP tool {name!r}. Use a registered tool name.", ok=False)
        client, remote_name, validator = self._routes[name]
        error = next(validator.iter_errors(arguments), None)
        if error is not None:
            result = ToolResult(
                f"Invalid arguments at {error.json_path}: {error.message}. "
                "This call was not executed. Correct the arguments.",
                ok=False,
            )
            self._trace.event("mcp_call", name=name, status="invalid_arguments", elapsed_seconds=0)
            return result
        started = time.monotonic()
        try:
            response = self._portal.call(
                partial(self._bounded, client.call_tool, remote_name, arguments)
            )
        except Exception as exc:
            # Remote boundary only: a timeout does not establish rollback/cancellation.
            self._trace.event(
                "mcp_call",
                name=name,
                status="transport_or_protocol_error",
                error_type=type(exc).__name__,
                error=str(exc),
                elapsed_seconds=time.monotonic() - started,
            )
            return ToolResult(
                f"MCP call failed ({type(exc).__name__}). Execution state is unknown. "
                "Check remote state before retrying a side-effecting operation; "
                "no automatic retry was performed. Technical details are in the audit trace.",
                ok=False,
            )
        raw = response.model_dump(mode="json", by_alias=True, exclude_none=True)
        result = _render(raw)
        self._trace.event(
            "mcp_call",
            name=name,
            status="ok" if result.ok else "result_error",
            response=raw,
            elapsed_seconds=time.monotonic() - started,
            rendered_chars=len(result.text),
            images=[image.describe() for image in result.images],
        )
        return result


@contextmanager
def open_mcp_tools(
    base: TaskTools, clients: dict[str, Client], trace: Trace, *, timeout: float = 60
):
    """Connect once, discover once, expose tools alongside base, close on every exit.

    Client settings/credentials belong to host code, never model arguments. Only connect
    operator-selected servers: external servers are not inside the coding sandbox.
    The surface is frozen until the next episode, even if a server's catalog changes.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MCP timeout must be finite and positive")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", n) for n in clients):
        raise ValueError(
            "MCP namespaces must be 1–20 ASCII letters, digits, underscores or hyphens"
        )
    with start_blocking_portal() as portal:
        stack = ExitStack()
        try:
            for client in clients.values():
                stack.enter_context(portal.wrap_async_context_manager(client))
            yield MCPTools(base, clients, portal, trace, timeout)
        finally:
            # Do not inject host/episode exceptions into the SDK's transport task groups.
            stack.close()
