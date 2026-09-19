Replace one exact, unique text occurrence in an existing file.
- Use after read_file shows the text to change; use write_file only for a new path.
- Re-read after a stale-file refusal; absent or ambiguous matches change nothing.
- Changes the workspace atomically. Use diff to review accumulated edits and run_tests to check behavior; edits are not proof of correctness.
<example>
After reading the current file, edit_file({"path":"src/parser.py","old_text":"return items[0]","new_text":"return items[0] if items else None"}) replaces that exact unique span. If it is ambiguous or stale, read again and supply enough surrounding text; do not guess. This illustrates the call format, not a suggested fix for your task.
</example>
