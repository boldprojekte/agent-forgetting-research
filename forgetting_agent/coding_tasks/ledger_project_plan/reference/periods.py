"""Read-only period views and immutable reconciled close snapshots."""
import json
import sqlite3
from contextlib import closing

import journal
import ledger
import reporting
from migration import atomic, migrate


def selected(connection, period):
    if not journal.exists(connection, "entry_periods"):
        return []
    return list(connection.execute(
        "SELECT e.id,e.category,e.amount_cents FROM entries e JOIN entry_periods p "
        "ON p.entry_id=e.id WHERE p.period=? ORDER BY e.id COLLATE BINARY", (period,)
    ))


def report(connection, period):
    journal.validate_period(period)
    if journal.is_closed(connection, period):
        return json.loads(connection.execute(
            "SELECT snapshot FROM period_closes WHERE period=?", (period,)
        ).fetchone()[0])
    rows = selected(connection, period)
    with closing(sqlite3.connect(":memory:")) as view:
        ledger.init_db(view)
        view.executemany("INSERT INTO entries VALUES (?,?,?)", rows)
        categories = reporting.summarize(view)
        text = reporting.export_csv(view)
    return dict(period=period, entry_count=len(rows), total_cents=sum(r[2] for r in rows),
                categories=categories, csv=text, closed=False)


def close(connection, period, expected_text):
    journal.validate_period(period)
    if not isinstance(expected_text, str):
        raise ValueError("expected CSV text")
    with atomic(connection):
        migrate(connection)
        prior = connection.execute(
            "SELECT expected_text,snapshot FROM period_closes WHERE period=?", (period,)
        ).fetchone()
        if prior is not None:
            if prior[0] != expected_text:
                raise ValueError("close identity conflict")
            return json.loads(prior[1])
        earlier = connection.execute(
            "SELECT DISTINCT p.period FROM entry_periods p LEFT JOIN period_closes c "
            "ON p.period=c.period WHERE p.period < ? AND c.period IS NULL", (period,)
        ).fetchall()
        if earlier:
            raise ValueError("earlier periods are open")
        _, expected = journal.parsed(expected_text)
        if selected(connection, period) != expected:
            raise ValueError("period reconciliation failed")
        snapshot = dict(report(connection, period), closed=True)
        connection.execute("INSERT INTO period_closes VALUES (?,?,?)",
                           (period, expected_text, json.dumps(snapshot)))
        return snapshot
