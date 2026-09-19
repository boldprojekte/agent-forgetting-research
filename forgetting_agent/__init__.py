"""Minimal research agent for agent-controlled context selection.

Modules:
- tools.py: task tools over a local corpus plus the model-facing tool schemas.
- context.py: active history, archive, search menus, set-aside and recovery transitions.
- loop.py: the explicit sequential episode loop with termination states.
- experiment.py: task fixtures, grading, arms, trace writer and size accounting.
- provider.py: thin OpenAI-compatible SDK adapter (live only).
- scripted.py: scripted model fixture for offline orchestration tests and demos.
- cli.py: command-line entry point.
"""
