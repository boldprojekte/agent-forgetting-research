"""Sandbox-only observations for the pilot ledger. Inputs, never answer keys.

Executed as source by coding.grade; do not import or execute candidate code on host.
"""

import importlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path


def rows(db):
    return [list(row) for row in db.execute(
        "SELECT id, category, amount_cents, rowid FROM entries ORDER BY id COLLATE BINARY"
    )]


def observe_legacy(fixture):
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "ledger.db")
        db = sqlite3.connect(path)
        db.execute("PRAGMA foreign_keys=ON")
        app = importlib.import_module("ledger")
        app.init_db(db)
        db.executemany("INSERT INTO entries VALUES (?, ?, ?)", fixture.get("seed", []))
        db.execute("CREATE TABLE links (entry_id TEXT REFERENCES entries(id) ON DELETE CASCADE)")
        db.executemany("INSERT INTO links VALUES (?)", fixture.get("links", []))
        db.execute("CREATE TABLE caller_work (value TEXT)")
        db.commit()
        if fixture.get("outer"):
            db.execute("BEGIN")
            db.execute("INSERT INTO caller_work VALUES ('owned-by-caller')")
        imports = []
        for text in fixture.get("imports", []):
            try:
                imports.append({"count": app.import_csv(db, text)})
            except Exception as error:
                imports.append({"error": type(error).__name__})
        reports = []
        for category in fixture.get("reports", []):
            try:
                reports.append(app.report(db, category))
            except Exception as error:
                reports.append({"error": type(error).__name__})
        with sqlite3.connect(path) as observer:
            persisted = rows(observer)
        result = {
            "imports": imports,
            "rows": rows(db),
            "links": [row[0] for row in db.execute("SELECT entry_id FROM links ORDER BY entry_id")],
            "reports": reports,
            "active": db.in_transaction,
            "persisted": persisted,
        }
        if fixture.get("outer"):
            result["caller_before_rollback"] = [
                list(row) for row in db.execute("SELECT * FROM caller_work")
            ]
            db.rollback()
            result["after_rollback"] = rows(db)
            result["caller_after_rollback"] = list(db.execute("SELECT * FROM caller_work"))
        db.close()
        return result


def observe(fixture):
    if not fixture.get("workflow"):
        return observe_legacy(fixture)
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "workflow.db")
        db = sqlite3.connect(path)
        db.execute("PRAGMA foreign_keys=ON")
        app = importlib.import_module("ledger")
        workflow = importlib.import_module("workflow")
        reporting = importlib.import_module("reporting")
        pipeline = importlib.import_module("pipeline")
        app.init_db(db)
        db.executemany("INSERT INTO entries VALUES (?, ?, ?)", fixture.get("seed", []))
        if fixture.get("retention"):
            db.execute(
                "CREATE TABLE links (entry_id TEXT REFERENCES entries(id) ON DELETE CASCADE)"
            )
            db.executemany("INSERT INTO links VALUES (?)", [(x,) for x in fixture.get("links", [])])
            db.execute("CREATE TABLE caller_work (value TEXT)")
        db.commit()
        if fixture.get("outer"):
            db.execute("BEGIN")
            if fixture.get("retention"):
                db.execute("INSERT INTO caller_work VALUES ('owned-by-caller')")
        results = []
        for action in fixture.get("actions", []):
            module = workflow if action[0] in ("ingest", "history") else reporting
            if action[0] == "run":
                module = pipeline
            try:
                results.append(getattr(module, action[0])(db, *action[1:]))
            except Exception as error:
                results.append({"error": type(error).__name__})
        with sqlite3.connect(path) as observer:
            persisted = rows(observer)
        result = dict(results=results, rows=rows(db), active=db.in_transaction,
                      persisted=persisted)
        if fixture.get("retention"):
            result["links"] = [r[0] for r in db.execute(
                "SELECT entry_id FROM links ORDER BY entry_id"
            )]
            result["caller"] = [r[0] for r in db.execute("SELECT value FROM caller_work")]
            with sqlite3.connect(path) as observer:
                result["persisted_history"] = workflow.history(observer)
        if fixture.get("outer"):
            db.rollback()
            result["after_rollback"] = rows(db)
            if fixture.get("retention"):
                result["after_history"] = workflow.history(db)
                result["after_caller"] = list(db.execute("SELECT value FROM caller_work"))
        db.close()
        return result


if __name__ == "__main__":
    job = json.load(sys.stdin)
    for case in job["cases"]:
        try:
            record = {"id": case["id"], "value": observe(case["fixture"])}
        except BaseException as error:
            record = {"id": case["id"], "error": f"{type(error).__name__}: {error}"[:300]}
        print(json.dumps(record), flush=True)
    print(json.dumps({"grader_done": len(job["cases"])}), flush=True)
