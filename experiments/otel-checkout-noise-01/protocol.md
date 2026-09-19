---
title: "OpenTelemetry checkout noise benchmark"
created: 2026-09-19
updated: 2026-09-19
type: experiment
tags: [research]
sources: ["source.json", "../../evidence/otel-checkout-noise-01.md"]
---

# OpenTelemetry checkout noise benchmark

## Purpose

This experiment tests whether agent-controlled context selection helps during a realistic
observability-heavy debugging trajectory. Noise is produced by a running public microservice
application with concurrent faults, background load, screenshots, traces, logs, metrics, source
inspection, builds and browser verification. It is not injected prose padding.

The benchmark uses the Apache-2.0 OpenTelemetry Demo at commit
`af3dc6553d9f7aebb5929b192fbf3a922a467050`, immediately before the real upstream checkout fix in
PR 3739. The private oracle is the merged upstream patch at commit
`586dd1c66ae0576c3840921a417eb104592ea6b1`.

## Controlled conditions

Both arms receive byte-identical source snapshots, the same task, model, reasoning setting,
output budget, tools, live stack configuration, active fault configuration and completion gate.
Only the context-management condition differs. Runs are sequential because the Docker stack uses
fixed service names and because simultaneous load would change the telemetry stream.

The target frontend and the checkout and payment services on its failing call path are built from
the pinned benchmark source. Other services use the official 3.1.0 release images, with resolved
digests retained in the evidence bundle. Every arm starts after a volume reset, atomic restoration
of the private flag seed, source-image rebuild and whole-stack health gate.

The stack enables payment failure, ad failure, slow images, Kafka queue problems, homepage load
flooding and one product-specific catalog failure. Only the payment failure is the target defect.
The remaining signals are authentic distractors that remain potentially relevant until the agent
has diagnosed the checkout path.

## Primary task

The agent must diagnose the live checkout failure from the application and observability surfaces,
then implement and verify an upstream-quality frontend fix. A successful implementation must:

1. return HTTP 422 with a safe non-empty message and `PAYMENT_FAILED` for the expected simulated
   payment decline;
2. keep unrelated internal failures generic HTTP 500 responses without a payment code or leaked
   upstream details;
3. make the shared request helper reject non-success responses, retain a JSON server message, and
   handle non-JSON error bodies safely;
4. preserve successful checkout behavior and error telemetry;
5. add a focused Cypress regression test and a factual `BENCHMARK_REPORT.md`.

The independent grader injects and runs the upstream regression test after the episode. The oracle
patch and grader stay outside the agent workspace.

The grader is `scripts/grade_otel_checkout.py`. It copies the submitted workspace, injects the
private upstream test only into that temporary copy, rebuilds the candidate through the trusted
Dockerfile and grades it against a freshly reset stack.

## Follow-up task

After a completed primary submission, the same conversation receives a real unrelated upstream
task based on PR 3871: graceful shutdown of the C++ currency service. This tests whether the agent
can clear obsolete frontend diagnosis material and pivot while retaining general repository and
tool knowledge. The follow-up oracle is the single-commit patch at `d3ee0736`.

## Measurements

Record final and maximum active prompt tokens, cumulative input/output/reasoning tokens, known
cost, wall time, provider attempts, tool calls, browser images, context operations and archive
bytes. Grade final behavior independently and retain the full trajectory. Separately report how
much context remained after the first task and before the follow-up request.

This is a development benchmark until both arms complete from separately reset live stacks. A
single pair supports only descriptive findings.
