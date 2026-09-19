"""Strict version-2 manifests and atomic multi-operation execution."""
import json

import journal
import periods
import reporting
from migration import atomic, migrate

FIELDS = {
    "post": {"kind", "operation_id", "period", "source"},
    "correct": {"kind", "operation_id", "entry_id", "category", "amount_cents"},
    "reverse": {"kind", "operation_id", "target_operation_id"},
    "close": {"kind", "period", "source"},
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode(text):
    if not isinstance(text, str):
        raise ValueError("expected JSON text")
    try:
        return json.loads(text, object_pairs_hook=unique_object)
    except json.JSONDecodeError as error:
        raise ValueError("invalid JSON") from error


def validate(manifest_text, sources):
    manifest = decode(manifest_text)
    if (not isinstance(manifest, dict) or not {"version", "operations"} <= manifest.keys()
            or manifest.keys() - {"version", "operations", "dry_run", "expected_source"}
            or type(manifest["version"]) is not int or manifest["version"] != 2
            or not isinstance(manifest["operations"], list)
            or type(manifest.get("dry_run", False)) is not bool
            or not isinstance(sources, dict)):
        raise ValueError("invalid project manifest")
    references = []
    if "expected_source" in manifest:
        references.append(manifest["expected_source"])
    for operation in manifest["operations"]:
        if (not isinstance(operation, dict) or not isinstance(operation.get("kind"), str)
                or operation["kind"] not in FIELDS
                or operation.keys() != FIELDS[operation["kind"]]):
            raise ValueError("invalid operation shape")
        for key, value in operation.items():
            if key == "amount_cents":
                if type(value) is not int or not 0 <= value <= 9223372036854775807:
                    raise ValueError("invalid amount")
            elif key == "period":
                journal.validate_period(value)
            else:
                journal.nonempty(value)
        if "source" in operation:
            references.append(operation["source"])
    for name in references:
        journal.nonempty(name)
        if name not in sources or not isinstance(sources[name], str):
            raise ValueError("missing or invalid source")
    return manifest


class _DryRun(Exception):
    """Internal control flow to roll back a successful tentative transaction."""


def run(connection, manifest_text, sources):
    manifest = validate(manifest_text, sources)
    try:
        with atomic(connection):
            migrate(connection)
            results = []
            for operation in manifest["operations"]:
                kind = operation["kind"]
                if kind == "post":
                    value = journal.post(connection, operation["operation_id"],
                                         operation["period"], sources[operation["source"]])
                elif kind == "correct":
                    value = journal.correct(connection, operation["operation_id"],
                                            operation["entry_id"], operation["category"],
                                            operation["amount_cents"])
                elif kind == "reverse":
                    value = journal.reverse(connection, operation["operation_id"],
                                            operation["target_operation_id"])
                else:
                    value = periods.close(connection, operation["period"],
                                          sources[operation["source"]])
                results.append(value)
            if "expected_source" in manifest:
                differences = reporting.reconcile(connection, sources[manifest["expected_source"]])
                if any(differences.values()):
                    raise ValueError("project reconciliation failed")
            result = dict(results=results, report=reporting.summarize(connection),
                          csv=reporting.export_csv(connection), events=journal.events(connection))
            if manifest.get("dry_run", False):
                raise _DryRun
    except _DryRun:
        pass
    return result
