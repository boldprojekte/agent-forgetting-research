"""Atomic schema upgrade, without rebuilding the legacy ledger."""
from contextlib import contextmanager
from uuid import uuid4

import ledger


@contextmanager
def atomic(connection):
    name = "project_" + uuid4().hex
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {name}")
        raise
    finally:
        connection.execute(f"RELEASE {name}")


def migrate(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 2:
        return 2
    if version not in (0, 1):
        raise ValueError("unsupported schema version")
    with atomic(connection):
        ledger.init_db(connection)
        for entry_id, category, amount in connection.execute("SELECT * FROM entries"):
            if (not isinstance(entry_id, str) or not entry_id
                    or not isinstance(category, str) or not category
                    or type(amount) is not int or not 0 <= amount <= 9223372036854775807):
                raise ValueError("invalid legacy entry")
        statements = [
            "CREATE TABLE IF NOT EXISTS entry_periods ("
            "entry_id TEXT PRIMARY KEY REFERENCES entries(id), period TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, "
            "request TEXT NOT NULL, receipt TEXT NOT NULL, reversed INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS journal_events (sequence INTEGER PRIMARY KEY, "
            "operation_id TEXT NOT NULL, kind TEXT NOT NULL, entry_id TEXT NOT NULL, "
            "period TEXT NOT NULL, before_state TEXT, after_state TEXT)",
            "CREATE TABLE IF NOT EXISTS period_closes (period TEXT PRIMARY KEY, "
            "expected_text TEXT NOT NULL, snapshot TEXT NOT NULL)",
        ]
        for statement in statements:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=2")
    return 2
