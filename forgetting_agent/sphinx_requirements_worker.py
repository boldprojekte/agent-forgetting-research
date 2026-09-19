"""Sandbox-only Sphinx build observer; receives scenarios, never expected answers."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path


class HTMLFacts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.tables = []
        self._link = None
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "#req-" in attrs.get("href", ""):
            self._link = [attrs["href"], ""]
        if tag == "table":
            self._table = []
        if tag == "tr" and self._table is not None:
            self._row = []
        if tag in ("td", "th") and self._row is not None:
            self._cell = ""

    def handle_data(self, data):
        if self._link is not None:
            self._link[1] += data
        if self._cell is not None:
            self._cell += data

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            self.links.append([self._link[0], " ".join(self._link[1].split())])
            self._link = None
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join(self._cell.split()))
            self._cell = None
        if tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        if tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def observe(fixture):
    from sphinx.application import Sphinx
    from sphinx.testing.util import _clean_up_global_state

    observations = []
    previous_export = None
    with tempfile.TemporaryDirectory(prefix="req-observe-") as tmp:
        root = Path(tmp)
        src, out, envdir = (root / n for n in ("src", "out", "env"))
        src.mkdir()
        config = {
            "extensions": ["sphinx.ext.requirements"],
            "master_doc": "index",
            "project": "Traceability",
            "html_theme": "basic",
            "exclude_patterns": [],
            "show_warning_types": True,
        }
        config.update(fixture.get("config", {}))
        (src / "conf.py").write_text("\n".join(f"{k} = {v!r}" for k, v in config.items()))
        for i, stage in enumerate(fixture["stages"]):
            for name, content in stage.get("docs", {}).items():
                path = src / (name + ".rst")
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                    stamp = time.time() + i * 5
                    os.utime(path, (stamp, stamp))
            status, warnings = io.StringIO(), io.StringIO()
            _clean_up_global_state()
            try:
                app = Sphinx(
                    src,
                    src,
                    out,
                    envdir,
                    fixture.get("builder", "html"),
                    status=status,
                    warning=warnings,
                    freshenv=i == 0 or stage.get("fresh", False),
                    parallel=stage.get("parallel", fixture.get("parallel", 1)),
                )
                app.build(force_all=False)
                export_path = out / "requirements.json"
                raw = export_path.read_bytes() if export_path.exists() else None
                export = json.loads(raw) if raw is not None else None
                domain = app.env.domains.get("req")
                warning_lines = [
                    line
                    for line in warnings.getvalue().splitlines()
                    if re.search(r"\[req\.[a-z_]+\]", line)
                ]
                item = {
                    "export": export,
                    "warnings": sorted(set(re.findall(r"\[req\.([a-z_]+)\]", warnings.getvalue()))),
                    "located": all(
                        re.search(r":\d+: WARNING:", line) is not None for line in warning_lines
                    ),
                }
                if fixture.get("objects"):
                    item["objects"] = (
                        sorted([list(v) for v in domain.get_objects()]) if domain else []
                    )
                if fixture.get("metadata"):
                    ext = app.extensions.get("sphinx.ext.requirements")
                    item["parallel_safe"] = [ext.parallel_read_safe, ext.parallel_write_safe]
                if stage.get("same_bytes"):
                    item["same_bytes"] = raw is not None and raw == previous_export
                previous_export = raw
                for doc in stage.get("html", []):
                    facts = HTMLFacts()
                    facts.feed((out / (doc + ".html")).read_text(encoding="utf-8"))
                    item[doc] = {"links": facts.links, "tables": facts.tables}
                for doc, needles in stage.get("contains", {}).items():
                    suffix = ".txt" if fixture.get("builder") == "text" else ".html"
                    text = (out / (doc + suffix)).read_text(encoding="utf-8")
                    item["contains:" + doc] = [needle in text for needle in needles]
                observations.append(item)
            except Exception as error:
                observations.append({"error_type": type(error).__name__})
            finally:
                _clean_up_global_state()
    return observations


def main():
    job = json.load(sys.stdin)
    for case in job["cases"]:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                value = observe(case["fixture"])
            record = {"id": case["id"], "value": value}
        except Exception as error:
            record = {"id": case["id"], "error": f"{type(error).__name__}: {error}"[:300]}
        print(json.dumps(record), flush=True)
    print(json.dumps({"grader_done": len(job["cases"])}), flush=True)


if __name__ == "__main__":
    main()
