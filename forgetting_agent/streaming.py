"""Assemble complete chat responses from SDK deltas before tools can execute."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class IncompleteStreamError(ValueError):
    """The response cannot safely enter the conversation history."""


class TruncatedStreamError(IncompleteStreamError):
    """Stream ended before a completion; safe to retry before acceptance."""


def _merge(target: dict[str, Any], delta: dict[str, Any]) -> None:
    for key, value in delta.items():
        if value is None:
            continue
        if isinstance(value, str) and key != "role":
            target[key] = target.get(key, "") + value
        elif isinstance(value, dict):
            _merge(target.setdefault(key, {}), value)
        else:
            target[key] = deepcopy(value)


class ChatStream:
    """Single completion accumulator; original chunks are recorded by the caller."""

    def __init__(self) -> None:
        self.body: dict[str, Any] = {"object": "chat.completion", "usage": None}
        self.choices: dict[int, dict[str, Any]] = {}
        self.calls: dict[int, dict[int, dict[str, Any]]] = {}

    def add(self, chunk: dict[str, Any]) -> None:
        for key, value in chunk.items():
            if key not in ("choices", "object") and value is not None:
                self.body[key] = deepcopy(value)
        for choice in chunk.get("choices", []):
            index = choice["index"]
            if type(index) is not int or index < 0:
                raise IncompleteStreamError("Invalid choice index")
            current = self.choices.setdefault(
                index,
                {
                    "index": index,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": None,
                },
            )
            delta = dict(choice.get("delta") or {})
            if current["finish_reason"] is not None and any(v for v in delta.values()):
                raise IncompleteStreamError("Content after finish reason")
            for call in delta.pop("tool_calls", None) or []:
                call = dict(call)
                call_index = call.pop("index")
                if type(call_index) is not int or call_index < 0:
                    raise IncompleteStreamError("Invalid tool index")
                target = self.calls.setdefault(index, {}).setdefault(call_index, {})
                # IDs/types identify the call; names and arguments are streamed fragments.
                for key in ("id", "type"):
                    value = call.pop(key, None)
                    if value is not None:
                        if key in target and target[key] != value:
                            raise IncompleteStreamError("Conflicting tool identity")
                        target[key] = value
                _merge(target, call)
            # Content starts as null for tool-only replies, then becomes concatenated text.
            if delta.get("content") is not None and current["message"]["content"] is None:
                current["message"]["content"] = ""
            _merge(current["message"], delta)
            for key, value in choice.items():
                if key not in ("index", "delta") and value is not None:
                    current[key] = deepcopy(value)

    def finish(self) -> dict[str, Any]:
        if not self.choices:
            raise TruncatedStreamError("Missing completion choices")
        if sorted(self.choices) != list(range(len(self.choices))):
            raise IncompleteStreamError("Noncontiguous completion choices")
        for index, choice in self.choices.items():
            if choice["finish_reason"] is None:
                raise TruncatedStreamError("Missing finish reason")
            if choice["finish_reason"] not in ("stop", "tool_calls"):
                raise IncompleteStreamError("Missing or unsuccessful finish reason")
            calls = self.calls.get(index, {})
            if choice["finish_reason"] == "tool_calls" and not calls:
                raise IncompleteStreamError("Tool finish without calls")
            if calls:
                if choice["finish_reason"] != "tool_calls":
                    raise IncompleteStreamError("Calls without tool finish")
                if sorted(calls) != list(range(len(calls))):
                    raise IncompleteStreamError("Missing tool fragments")
                for call in calls.values():
                    function = call.get("function", {})
                    if (
                        not call.get("id")
                        or call.get("type") != "function"
                        or not function.get("name")
                    ):
                        raise IncompleteStreamError("Incomplete tool call")
                    # A finished response may contain invalid arguments. Preserve them
                    # verbatim so the dispatcher can return a paired correction message.
                choice["message"]["tool_calls"] = [calls[i] for i in sorted(calls)]
        return {**self.body, "choices": [self.choices[i] for i in sorted(self.choices)]}
