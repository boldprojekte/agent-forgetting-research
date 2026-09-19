"""Evaluator-prescribed offline replay. Never used to supply an autonomous model."""

from .coding import CodingTask


def sqlite_csv_demo(task: CodingTask) -> list:
    reference = task.grader_path.parent / "reference/ledger.py"
    return [
        [("list_files", {})],
        [("read_file", {"path": "README.md"})],
        [("read_file", {"path": "ledger.py"})],
        [("run_tests", {"tests": ["tests"]})],
        [("grep", {"pattern": "transaction", "path_glob": "docs/sqlite3.rst",
                    "max_results": 8, "context_lines": 1})],
        [("read_file", {"path": "docs/lang_savepoint.in", "max_lines": 80})],
        [("grep", {"pattern": "excluded", "path_glob": "docs/lang_upsert.in",
                    "context_lines": 2})],
        [("grep", {"pattern": "placeholder", "path_glob": "docs/sqlite3.rst",
                    "max_results": 6})],
        [("edit_file", {"path": "ledger.py",
                        "old_text": (task.snapshot_dir / "ledger.py").read_text(),
                        "new_text": reference.read_text()})],
        [("write_file", {"path": "tests/test_regression.py", "content": '''import sqlite3

import pytest

import ledger


def test_import_failure_preserves_prior_value():
    with sqlite3.connect(":memory:") as db:
        ledger.init_db(db)
        ledger.import_csv(db, "id,category,amount_cents\\na,old,10\\n")
        with pytest.raises(ValueError):
            ledger.import_csv(db, "id,category,amount_cents\\na,new,20\\nb,z,-1\\n")
        assert ledger.report(db) == [dict(category="old", entry_count=1, total_cents=10)]
'''})],
        [("run_tests", {"tests": ["tests"]})],
        [("diff", {})],
        [("submit_answer", {"answer": "Evaluator-prescribed reference replay only. "
                            "Original and added regression tests pass in the sandbox."})],
    ]
