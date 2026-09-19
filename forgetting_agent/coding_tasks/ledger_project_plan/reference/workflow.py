"""Transactional ingestion with an auditable batch registry."""
import ledger


def ingest(connection, batch_id, text):
    if not isinstance(batch_id, str) or not batch_id or not isinstance(text, str):
        raise ValueError("batch_id must be nonempty text and source must be text")
    connection.execute("SAVEPOINT workflow_batch")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS batches ("
            "batch_id TEXT PRIMARY KEY, source TEXT NOT NULL, row_count INTEGER NOT NULL)"
        )
        previous = connection.execute(
            "SELECT source, row_count FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if previous is not None:
            if previous[0] != text:
                raise ValueError("batch ID already used for different source")
            count = previous[1]
        else:
            count = ledger.import_csv(connection, text)
            connection.execute("INSERT INTO batches VALUES (?, ?, ?)", (batch_id, text, count))
    except BaseException:
        connection.execute("ROLLBACK TO workflow_batch")
        connection.execute("RELEASE workflow_batch")
        raise
    connection.execute("RELEASE workflow_batch")
    return dict(batch_id=batch_id, row_count=count, replayed=previous is not None)


def history(connection):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batches'"
    ).fetchone():
        return []
    return [dict(batch_id=b, row_count=n) for b, n in connection.execute(
        "SELECT batch_id, row_count FROM batches ORDER BY batch_id COLLATE BINARY"
    )]
