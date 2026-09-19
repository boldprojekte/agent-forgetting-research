"""Audit trace: complete, inspectable JSONL separate from the model's active context.

Every event is kept in memory and, when a path is given, appended to a JSONL file and flushed
after each write. The trace is not a transaction log: a run whose trace is incomplete is
discarded, not repaired (no crash-resume guarantee).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .costs import estimate


class Trace:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.events: list[dict[str, Any]] = []
        self._cost_events: list[dict[str, Any]] = []
        self._file = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")

    def event(self, event: str, **payload: Any) -> None:
        record = {"seq": len(self.events), "t": time.time(), "event": event, **payload}
        self.events.append(record)
        if self._file is not None:
            self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._file.flush()

        if event in {"run_start", "request", "response", "provider_attempt", "termination"}:
            self._cost_events.append(record)
            if self.path is not None and event != "request":
                cost_path = self.path.parent / "costs.json"
                temporary = cost_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(estimate(self._cost_events), indent=2) + "\n")
                temporary.replace(cost_path)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def json_size(body: Any) -> tuple[int, int]:
    """(chars, utf-8 bytes) of the JSON serialisation. These are sizes, not token counts."""
    text = json.dumps(body, ensure_ascii=False, default=str)
    return len(text), len(text.encode("utf-8"))
