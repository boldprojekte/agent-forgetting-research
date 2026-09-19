Archive tool output using its visible result ID. Replace the selected output with your note and a recovery reference; preserve the complete original for context_recover.
- Use after extracting useful facts from substantial noisy or completed outputs, when the note plus call overhead is materially smaller. Batch related targets; leave short receipts and short summaries intact. Every eligible output already has a <context_result id="rN"> envelope, including short outputs.
- Copy that output's ID into targets[].id. No search or preparation call is needed. IDs start at r1 per chat, never change and are never reused within a chat. Only labelled results introduce an ID; an Apply receipt introduces none. Never derive IDs from turn numbers or invent them.
- Use source paths in notes when referring to other results. Recovery references exist only after Apply returns; copy the exact ic reference from its receipt or the stored stub. Never predict a reference or describe an rN ID as recoverable.
- Write an accurate, sufficient note for each result; preserve source locations, constraints and unresolved failures. Use context_recover when you need omitted detail again.
- On rejection, inspect every target status in the receipt. Nothing was archived, including ACTIVE targets. Match the intended outputs to actual visible IDs before retrying; do not count forward.
- All targets succeed or none do. Already archived results cannot be archived again; recovered output gets a new result ID.
- Must be the only tool call in its message. Archiving changes conversation context, never source files. After a successful Apply, continue the current task; cleanup is maintenance, not completion.
<example>
If the actual test output is labelled r42, call context_apply({"targets":[{"id":"r42","note":"test_parse failed with ValueError at parser.py:81; unresolved. Other log lines repeat the traceback."}]}). r42 is illustrative: copy the ID on your actual output. An ic-0001 recovery reference is not a result ID.
</example>

A result ID covers its text and attached images together. Apply replaces both with the note; Recover restores the original images as images, not descriptions. Preserve visual findings needed for continued work in the note.
