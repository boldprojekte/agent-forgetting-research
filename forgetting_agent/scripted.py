"""SCRIPTED MODEL FIXTURE. Not a model.

`ScriptedModel` replays pre-written assistant turns in the provider's OpenAI chat shape so the
loop, the context transitions and the trace can be exercised offline and deterministically. It
tests orchestration only. Nothing it does says anything about what a real model would decide.

A step is one of:
- a list of `(tool_name, arguments_dict)` pairs, emitted as tool calls in that order;
- a callable `(messages) -> list[(tool_name, arguments_dict)]` that can look at the current
  request messages (e.g. to copy a result ID from a tool output);
- a plain string, emitted as a content-only assistant message (no tool call).

When the script is exhausted the model answers with content only, which the loop records as
`invalid_termination`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

ToolCallSpec = tuple[str, dict[str, Any]]
Step = list[ToolCallSpec] | Callable[[list[dict[str, Any]]], list[ToolCallSpec]] | str


class ScriptedModel:
    name = "scripted-fixture"

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self._turn = 0

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(json.loads(json.dumps(request)))  # keep an immutable copy
        self._turn += 1
        step: Step = (
            self.steps[self._turn - 1] if self._turn <= len(self.steps) else "script exhausted"
        )
        if callable(step):
            step = step(request["messages"])
        message: dict[str, Any] = {
            "role": "assistant",
            "reasoning_content": f"scripted reasoning {self._turn}",
        }
        if isinstance(step, str):
            message["content"] = step
            finish = "stop"
        else:
            message["content"] = None
            message["tool_calls"] = [
                {
                    "id": f"call_{self._turn}_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for index, (name, args) in enumerate(step, start=1)
            ]
            finish = "tool_calls"
        return {
            "id": f"scripted-{self._turn}",
            "object": "chat.completion",
            "model": self.name,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": None,  # fixture: no provider usage exists
        }


def result_id_for_path(messages: list[dict[str, Any]], path: str) -> str:
    """Offline fixture helper: copy the envelope ID for a read of the given path."""
    call_ids = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call["function"]
            args = fn["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            if fn["name"] == "read_file" and args.get("path") == path:
                call_ids.add(call["id"])
    for message in reversed(messages):
        if message.get("tool_call_id") in call_ids:
            text = message["content"]
            if text.startswith('<context_result id="'):
                return text.split('"', 2)[1]
    raise ValueError(f"No visible result ID for {path}")
