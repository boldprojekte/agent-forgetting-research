"""Private observation worker: inputs only; expected outcomes remain on the host."""

import contextlib
import io
import json
import os
import tempfile
import time
from pathlib import Path


def observe(fixture):
    from sphinx.application import Sphinx
    from sphinx.testing.util import _clean_up_global_state

    observations = []
    previous = None
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        src, out, envdir = [root / n for n in ("src", "out", "env")]
        src.mkdir()
        config = {
            "extensions": ["sphinx.ext.publish_manifest"],
            "master_doc": "index",
            "project": "Manifest check",
            "html_theme": "basic",
            "exclude_patterns": [],
        }
        config.update(fixture.get("config", {}))
        if fixture.get("tuple_exclude"):
            config["publish_manifest_exclude"] = tuple(config["publish_manifest_exclude"])
        for i, stage in enumerate(fixture["stages"]):
            config.update(stage.get("config", {}))
            (src / "conf.py").write_text("\n".join(f"{k} = {v!r}" for k, v in config.items()))
            for name, text in stage.get("docs", {}).items():
                p = src / (name + ".rst")
                if text is None:
                    p.unlink(missing_ok=True)
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(text, encoding="utf-8")
                    stamp = time.time() + i * 10
                    os.utime(p, (stamp, stamp))
            status, warning = io.StringIO(), io.StringIO()
            _clean_up_global_state()
            try:
                app = Sphinx(
                    src,
                    src,
                    out,
                    envdir,
                    fixture.get("builder", "html"),
                    status=status,
                    warning=warning,
                    freshenv=i == 0 or stage.get("fresh", False),
                    parallel=stage.get("parallel", fixture.get("parallel", 1)),
                )
                app.build(force_all=False)
                path = out / "publish_manifest.json"
                raw = path.read_bytes() if path.exists() else None
                result = {
                    "export": json.loads(raw) if raw else None,
                    "newline": bool(raw and raw.endswith(b"\n")),
                    "no_escaped_unicode": bool(raw and b"\\u" not in raw),
                }
                if stage.get("same_bytes"):
                    result["same_bytes"] = raw is not None and raw == previous
                if fixture.get("metadata"):
                    ext = app.extensions.get("sphinx.ext.publish_manifest")
                    result["parallel_safe"] = (
                        [ext.parallel_read_safe, ext.parallel_write_safe] if ext else None
                    )
                if stage.get("exception_preserves"):
                    path.write_text("SENTINEL\n")
                    app.emit("build-finished", RuntimeError("injected unsuccessful build"))
                    result["exception_preserves"] = path.read_text() == "SENTINEL\n"
                if stage.get("absent"):
                    suffix = ".txt" if fixture.get("builder") == "text" else ".html"
                    content = (out / ("index" + suffix)).read_text()
                    result["absent"] = [needle not in content for needle in stage["absent"]]
                observations.append(result)
                previous = raw
            except Exception as error:
                observations.append({"error_type": type(error).__name__})
            finally:
                _clean_up_global_state()
    return observations


def main():
    import sys

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
