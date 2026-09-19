Find matching lines across workspace files.
- Use when a symbol, diagnostic or text fragment is known but its location is not.
- Use read_file to inspect complete lines before editing; use list_files for paths.
<example>
grep({"pattern":"parse_config","path_glob":"src/**/*.py"}) finds literal occurrences. Read the relevant match with read_file before choosing a change.
</example>
