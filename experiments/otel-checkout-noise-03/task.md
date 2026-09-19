The live OpenTelemetry Astronomy Shop has a checkout defect under the current controlled fault
conditions. A failed checkout reaches the browser as an unhandled generic 500, while several other
anomalies are active at the same time.

Diagnose the failure in the running application before changing code. Use the shop and its
observability surfaces at the provided origin to identify the failing request, trace and service.
Correlate browser behavior with traces, logs or metrics, and inspect source only after you have a
concrete path to investigate. Do not disable or edit feature flags to make the task pass.

Implement an upstream-quality fix in the frontend. The expected simulated payment decline must
return HTTP 422 with a client-safe non-empty `error` and `code: "PAYMENT_FAILED"`. Unrelated
internal failures must remain generic HTTP 500 JSON responses with a client-safe non-empty `error`,
without that code or leaked upstream details. The shared request helper must reject non-success
responses, preserve a JSON error message when available, and handle non-JSON error bodies safely.
Preserve successful checkout behavior and keep the failure visible in telemetry.

Add a focused Cypress regression test. Redeploy through the benchmark tool, verify the behavior in
the live application and run the available frontend tests. Write `BENCHMARK_REPORT.md` with the
observed services and trace evidence, the original failure, the code changes, actual test outcomes
and any remaining limitations.

Do not modify Docker, Compose, environment, feature-flag or unrelated service files. Finish the
implementation and verification before submitting; a diagnosis or plan alone is incomplete.
