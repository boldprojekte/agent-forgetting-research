"""Strict manifest validation and one atomic multi-source workflow."""
import json

import reporting
import workflow


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _manifest(text, sources):
    if not isinstance(text, str) or not isinstance(sources, dict):
        raise ValueError("expected manifest text and source mapping")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        raise ValueError("malformed manifest JSON") from error
    allowed = {"version", "batches", "dry_run", "export_category", "expected_source"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("unknown manifest shape")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("unsupported manifest version")
    if not isinstance(value.get("batches"), list):
        raise ValueError("batches must be a list")
    if type(value.get("dry_run", False)) is not bool:
        raise ValueError("dry_run must be boolean")
    if value.get("export_category") is not None and not isinstance(value["export_category"], str):
        raise ValueError("export_category must be text or null")
    needed = []
    for batch in value["batches"]:
        if not isinstance(batch, dict) or set(batch) != {"batch_id", "source"}:
            raise ValueError("batch requires exactly batch_id and source")
        if any(not isinstance(batch[k], str) or not batch[k] for k in batch):
            raise ValueError("batch fields must be nonempty strings")
        needed.append(batch["source"])
    if "expected_source" in value:
        if not isinstance(value["expected_source"], str) or not value["expected_source"]:
            raise ValueError("expected_source must be nonempty text")
        needed.append(value["expected_source"])
    if any(key not in sources or not isinstance(sources[key], str) for key in needed):
        raise ValueError("missing or non-text source")
    return value


def run(connection, manifest_text, sources):
    manifest = _manifest(manifest_text, sources)
    connection.execute("SAVEPOINT pipeline_run")
    try:
        batches = [workflow.ingest(connection, batch["batch_id"], sources[batch["source"]])
                   for batch in manifest["batches"]]
        reconciliation = None
        if "expected_source" in manifest:
            reconciliation = reporting.reconcile(connection, sources[manifest["expected_source"]])
            if any(reconciliation.values()):
                raise ValueError("reconciliation failed")
        result = dict(batches=batches, report=reporting.summarize(connection),
                      csv=reporting.export_csv(connection, manifest.get("export_category")),
                      reconciliation=reconciliation)
        if manifest.get("dry_run", False):
            connection.execute("ROLLBACK TO pipeline_run")
    except BaseException:
        connection.execute("ROLLBACK TO pipeline_run")
        connection.execute("RELEASE pipeline_run")
        raise
    connection.execute("RELEASE pipeline_run")
    return result
