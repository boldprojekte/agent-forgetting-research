"""Prescribed offline module integration and repair, never an autonomous model."""


def ledger_workflow_demo(task):
    steps = [
        [("list_files", {})],
        [("read_file", {"path": "README.md"})],
        [("grep", {"pattern": "SAVEPOINT", "path_glob": "docs/lang_savepoint.in",
                    "max_results": 5})],
        [("read_file", {"path": "docs/json.rst", "max_lines": 60})],
        [("run_tests", {"tests": ["tests"]})],
    ]
    source = task.grader_path.parent / "reference"
    for name in ("workflow.py", "reporting.py", "pipeline.py"):
        text = (source / name).read_text()
        if name == "pipeline.py":
            text = text.replace('if manifest.get("dry_run", False):', 'if False:')
        steps += [
            [("read_file", {"path": name})],
            [("edit_file", {"path": name,
                            "old_text": (task.snapshot_dir / name).read_text(), "new_text": text})],
            [("run_tests", {"tests": ["tests"]})],
        ]
    steps += [
        [("read_file", {"path": "pipeline.py"})],
        [("edit_file", {"path": "pipeline.py", "old_text": "if False:",
                        "new_text": 'if manifest.get("dry_run", False):'})],
        [("run_tests", {"tests": ["tests"]})],
        [("diff", {})],
        [("submit_answer", {"answer": "Prescribed offline reference integration and dry-run "
                            "repair only. Not autonomous model evidence."})],
    ]
    return steps
