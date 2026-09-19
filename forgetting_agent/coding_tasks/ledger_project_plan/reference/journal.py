"""Durable request identity and deterministic before/after journal."""
import json
import re
import sqlite3
from contextlib import closing

import ledger
from migration import atomic, migrate


def nonempty(value):
    if not isinstance(value, str) or not value:
        raise ValueError("expected nonempty text")


def validate_period(period):
    if (not isinstance(period, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", period)
            or period[:4] == "0000" or not 1 <= int(period[5:]) <= 12):
        raise ValueError("expected YYYY-MM period")


def exists(connection, table):
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def parsed(text):
    if not isinstance(text, str):
        raise ValueError("expected CSV text")
    with closing(sqlite3.connect(":memory:")) as db:
        ledger.init_db(db)
        count = ledger.import_csv(db, text)
        rows = list(db.execute("SELECT * FROM entries ORDER BY id COLLATE BINARY"))
    return count, rows


def state(connection, entry_id):
    row = connection.execute(
        "SELECT category, amount_cents FROM entries WHERE id=?", (entry_id,)
    ).fetchone()
    return None if row is None else dict(category=row[0], amount_cents=row[1])


def is_closed(connection, period):
    return exists(connection, "period_closes") and bool(connection.execute(
        "SELECT 1 FROM period_closes WHERE period=?", (period,)
    ).fetchone())


def require_open(connection, period):
    if is_closed(connection, period):
        raise ValueError("period is closed")


def replay(connection, operation_id, request):
    previous = connection.execute(
        "SELECT request, receipt FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if previous is None:
        return None
    if previous[0] != json.dumps(request, ensure_ascii=False):
        raise ValueError("operation ID conflict")
    return dict(json.loads(previous[1]), replayed=True)


def receipt(connection, operation_id, request, count):
    value = dict(operation_id=operation_id, kind=request[0], row_count=count, replayed=False)
    connection.execute(
        "INSERT INTO operations(operation_id,request,receipt) VALUES (?,?,?)",
        (operation_id, json.dumps(request, ensure_ascii=False), json.dumps(value)),
    )
    return value


def append_event(connection, operation_id, kind, entry_id, period, before, after):
    connection.execute(
        "INSERT INTO journal_events(operation_id,kind,entry_id,period,before_state,after_state) "
        "VALUES (?,?,?,?,?,?)",
        (operation_id, kind, entry_id, period, json.dumps(before), json.dumps(after)),
    )


def post(connection, operation_id, period, text):
    nonempty(operation_id)
    validate_period(period)
    if not isinstance(text, str):
        raise ValueError("expected CSV text")
    request = ["post", period, text]
    with atomic(connection):
        migrate(connection)
        previous = replay(connection, operation_id, request)
        if previous is not None:
            return previous
        require_open(connection, period)
        count, rows = parsed(text)
        for entry_id, category, amount in rows:
            assigned = connection.execute(
                "SELECT period FROM entry_periods WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if assigned is not None and assigned[0] != period:
                raise ValueError("entry belongs to another period")
            before = state(connection, entry_id)
            connection.execute(
                "INSERT INTO entries VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "category=excluded.category,amount_cents=excluded.amount_cents",
                (entry_id, category, amount),
            )
            connection.execute(
                "INSERT OR IGNORE INTO entry_periods VALUES (?,?)", (entry_id, period)
            )
            append_event(connection, operation_id, "post", entry_id, period, before,
                         dict(category=category, amount_cents=amount))
        return receipt(connection, operation_id, request, count)


def events(connection, entry_id=None):
    if not exists(connection, "journal_events"):
        return []
    where = "" if entry_id is None else " WHERE entry_id=?"
    rows = connection.execute(
        "SELECT * FROM journal_events" + where + " ORDER BY sequence",
        () if entry_id is None else (entry_id,),
    )
    return [dict(sequence=s, operation_id=o, kind=k, entry_id=i, period=p,
                 before=json.loads(b), after=json.loads(a)) for s, o, k, i, p, b, a in rows]


def correct(connection, operation_id, entry_id, category, amount_cents):
    for value in (operation_id, entry_id, category):
        nonempty(value)
    if type(amount_cents) is not int or not 0 <= amount_cents <= 9223372036854775807:
        raise ValueError("invalid amount")
    request = ["correct", entry_id, category, amount_cents]
    with atomic(connection):
        migrate(connection)
        previous = replay(connection, operation_id, request)
        if previous is not None:
            return previous
        assigned = connection.execute(
            "SELECT period FROM entry_periods WHERE entry_id=?", (entry_id,)
        ).fetchone()
        before = state(connection, entry_id)
        if assigned is None or before is None:
            raise ValueError("unknown or unassigned entry")
        period = assigned[0]
        require_open(connection, period)
        after = dict(category=category, amount_cents=amount_cents)
        connection.execute("UPDATE entries SET category=?,amount_cents=? WHERE id=?",
                           (category, amount_cents, entry_id))
        append_event(connection, operation_id, "correct", entry_id, period, before, after)
        return receipt(connection, operation_id, request, 1)


def reverse(connection, operation_id, target_operation_id):
    nonempty(operation_id)
    nonempty(target_operation_id)
    request = ["reverse", target_operation_id]
    with atomic(connection):
        migrate(connection)
        previous = replay(connection, operation_id, request)
        if previous is not None:
            return previous
        target = connection.execute(
            "SELECT receipt,reversed FROM operations WHERE operation_id=?", (target_operation_id,)
        ).fetchone()
        if target is None or json.loads(target[0])["kind"] != "correct" or target[1]:
            raise ValueError("target is not an unreversed correction")
        event = connection.execute(
            "SELECT sequence,entry_id,period,before_state,after_state FROM journal_events "
            "WHERE operation_id=?", (target_operation_id,)
        ).fetchone()
        sequence, entry_id, period, before_json, after_json = event
        latest = connection.execute(
            "SELECT MAX(sequence) FROM journal_events WHERE entry_id=?", (entry_id,)
        ).fetchone()[0]
        if latest != sequence:
            raise ValueError("target was superseded")
        require_open(connection, period)
        before, after = json.loads(before_json), json.loads(after_json)
        connection.execute("UPDATE entries SET category=?,amount_cents=? WHERE id=?",
                           (before["category"], before["amount_cents"], entry_id))
        append_event(connection, operation_id, "reverse", entry_id, period, after, before)
        connection.execute("UPDATE operations SET reversed=1 WHERE operation_id=?",
                           (target_operation_id,))
        return receipt(connection, operation_id, request, 1)
