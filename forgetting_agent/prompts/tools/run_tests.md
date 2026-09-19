Run selected pytest tests in an isolated, network-free sandbox.
- Use a focused selection to reproduce a failure or verify a fix. A successful tool call is not a passing suite: inspect the reported exit status.
- For a hang or missing diagnostic output, set diagnostic=true: test names and prints are captured without pytest hiding passing-test output. Slow tests dump Python thread stacks before the outer timeout kills the process tree.
- The returned test-output: ID opens the captured log through read_file, including content omitted from the inline preview. Capture limits still apply and are stated; do not claim omitted bytes were inspected.
- Each call starts a fresh sandbox. /tmp and process state do not persist between calls. Workspace changes are imported only as reported; re-read changed files before editing.
- Record the observed failure, test one concrete explanation, and inspect the result before repeating. Do not turn a diagnostic into a passing test unless it checks the behavior that was failing.
