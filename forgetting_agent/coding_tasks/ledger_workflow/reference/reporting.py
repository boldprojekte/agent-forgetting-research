"""Read-only, deterministic interchange and reconciliation."""
import csv
import io
import sqlite3

import ledger


def export_csv(connection, category=None):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", "category", "amount_cents"])
    where = "" if category is None else " WHERE category = ?"
    writer.writerows(connection.execute(
        "SELECT id, category, amount_cents FROM entries" + where + " ORDER BY id COLLATE BINARY",
        () if category is None else (category,),
    ))
    return output.getvalue()


def summarize(connection):
    groups = {}
    for category, amount in connection.execute("SELECT category, amount_cents FROM entries"):
        group = groups.setdefault(category, dict(category=category, entry_count=0,
                                  total_cents=0, min_cents=amount, max_cents=amount))
        group["entry_count"] += 1
        group["total_cents"] += amount
        group["min_cents"] = min(group["min_cents"], amount)
        group["max_cents"] = max(group["max_cents"], amount)
    return [groups[key] for key in sorted(groups)]


def reconcile(connection, text):
    # Reuse the same strict parser without touching the caller's database or transaction.
    with sqlite3.connect(":memory:") as expected_db:
        ledger.init_db(expected_db)
        ledger.import_csv(expected_db, text)
        expected = {key: dict(category=c, amount_cents=n) for key, c, n in expected_db.execute(
            "SELECT id, category, amount_cents FROM entries"
        )}
    actual = {key: dict(category=c, amount_cents=n) for key, c, n in connection.execute(
        "SELECT id, category, amount_cents FROM entries"
    )}
    return dict(
        missing=sorted(expected.keys() - actual.keys()),
        unexpected=sorted(actual.keys() - expected.keys()),
        changed=[dict(id=key, expected=expected[key], actual=actual[key])
                 for key in sorted(expected.keys() & actual.keys())
                 if expected[key] != actual[key]],
    )
