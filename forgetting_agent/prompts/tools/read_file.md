Read a bounded window from a workspace text file or captured test-output: artifact.
- Copy test-output: IDs from run_tests. They are read-only logs, not workspace paths; the same line/column continuations apply.
- Use after a grep hit, before edit_file, or when a returned continuation is needed.
- Use grep to locate content across files; use list_files when the path is unknown.
- Read the needed continuation before quoting or editing a truncated line.
<example>
read_file({"path":"src/parser.py","start_line":40,"max_lines":80}) reads a relevant section. If output gives a continuation cursor, use that cursor to read the missing section before editing it.
</example>
