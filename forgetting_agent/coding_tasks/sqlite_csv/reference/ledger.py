"""Pilot-authored small CSV ledger; baseline implementation."""

import csv
import io


def init_db(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS entries ("
        "id TEXT PRIMARY KEY, category TEXT NOT NULL, amount_cents INTEGER NOT NULL)"
    )


def import_csv(connection, text):
    count = 0
    connection.execute("SAVEPOINT ledger_import")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if (reader.fieldnames is None or len(reader.fieldnames) != 3
                or set(reader.fieldnames) != {"id", "category", "amount_cents"}):
            raise ValueError("expected id,category,amount_cents header")
        for row in reader:
            amount = row.get("amount_cents")
            if (None in row or not row.get("id") or not row.get("category")
                    or not amount or not amount.isascii() or not amount.isdecimal()
                    or int(amount) > 9223372036854775807):
                raise ValueError("invalid ledger row")
            connection.execute(
                "INSERT INTO entries VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "category=excluded.category, amount_cents=excluded.amount_cents",
                (row["id"], row["category"], int(row["amount_cents"])),
            )
            count += 1
    except BaseException as error:
        connection.execute("ROLLBACK TO ledger_import")
        connection.execute("RELEASE ledger_import")
        if isinstance(error, csv.Error):
            raise ValueError("malformed CSV") from error
        raise
    connection.execute("RELEASE ledger_import")
    return count


def report(connection, category=None):
    where = "" if category is None else " WHERE category = ?"
    rows = connection.execute(
        "SELECT category, COUNT(*), SUM(amount_cents) FROM entries"
        + where + " GROUP BY category ORDER BY category COLLATE BINARY",
        () if category is None else (category,),
    )
    return [dict(category=c, entry_count=n, total_cents=t) for c, n, t in rows]
