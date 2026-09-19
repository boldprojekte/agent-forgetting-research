---
title: "OpenTelemetry checkout noise benchmark, final repaired replication"
created: 2026-09-19
updated: 2026-09-19
type: experiment
tags: [research]
sources: ["../otel-checkout-noise-01/source.json", "../otel-checkout-noise-02/protocol.md"]
---

# OpenTelemetry checkout noise benchmark, final repaired replication

This protocol inherits all controls and repairs from `otel-checkout-noise-02`. A retained
development run exposed two remaining semantic ambiguities before its paired method arm began.
That run is excluded from the final pair.

The follow-up contract now states that the single overall shutdown deadline starts when the signal
is received and covers both server draining and all telemetry work. It also states that server
startup failure exits nonzero without waiting for a signal. The independent grader starts a second
currency process on the occupied port to test this failure path, in addition to candidate build,
PID 1, SIGTERM, SIGINT, exit code, elapsed time, provider ownership, shared-deadline structure and
report checks. The baseline must fail 0/6 and upstream gold must pass 6/6 before either final arm.

Run retained first and method second from independently reset stacks and byte-identical source
snapshots. Use fresh run directories and no selective continuation or candidate repair. Grade both
submissions independently. Report quality, active context, cumulative usage, estimated cost,
requests, wall time and reversible-archive integrity. One pair remains descriptive.
