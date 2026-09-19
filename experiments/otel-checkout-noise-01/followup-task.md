Continue in this same repository and session with a separate production issue. The C++ currency
service currently relies on abrupt process termination. Implement graceful SIGTERM and SIGINT
handling so container shutdown stops accepting work, gives the gRPC server a bounded opportunity
to finish, and flushes and shuts down tracing, metrics and logging providers within one bounded
overall shutdown budget. Startup failures must still fail normally, and the container command must
forward termination signals to the service process.

Keep the implementation race-safe and suitable for Linux containers. Update the focused build
configuration and tests or verification needed for the behavior. Record the exact commands and
outcomes in `BENCHMARK_REPORT.md`, preserving the earlier report as a separate section. Do not
submit a design only; implement and verify the change.
