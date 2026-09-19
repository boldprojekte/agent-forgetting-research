Create a new workspace file and any missing parent directories.
- Use when the task needs a file at a path that does not exist.
- Existing files are refused; use read_file then edit_file instead of overwriting.
- Use diff to review the addition and run_tests to verify behavior.
