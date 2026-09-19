Continue in this same repository and session with a separate production issue. The C++ currency
service currently relies on abrupt process termination. Implement graceful SIGTERM and SIGINT
handling so container shutdown stops accepting work, gives the gRPC server a bounded opportunity
to finish, and flushes and shuts down tracing, metrics and logging providers within one bounded
overall shutdown deadline. That deadline must begin when the termination signal is received and
must include both the server drain and every telemetry flush and shutdown operation.

Startup failures must exit nonzero without waiting for a termination signal. The container command
must forward termination signals to the service process. Keep the implementation race-safe and
suitable for Linux containers. Update the focused build configuration and provider initialization
code needed to retain concrete SDK providers for shutdown. Build the candidate and verify shutdown
behavior through the benchmark tools.

Record the exact commands and outcomes in `BENCHMARK_REPORT.md`, preserving the earlier report as a
separate section. Do not submit a design only; implement and verify the change.
