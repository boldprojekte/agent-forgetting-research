# Benchmark Report — Currency Service Graceful Shutdown

Origin: http://127.0.0.1:18080 (OpenTelemetry Astronomy Shop)

## 1. Problem

The C++ currency service (`src/currency`) relied on abrupt termination:

- `RunServer` called `server->Wait()` with no SIGTERM/SIGINT handling at all — `docker stop`
  hit the process with default dispositions; the shell entrypoint
  (`sh -c "./usr/local/bin/currency ${CURRENCY_PORT}"`) did not forward signals either.
- No gRPC drain window and no tracer/meter/logger provider flush or shutdown; the concrete SDK
  providers were only held in the global API registries, so nothing could be shut down.
- Startup failures were unhandled: a failed `BuildAndStart` (e.g. occupied port) would dereference
  a null server/logger and the process did not exit nonzero deterministically. Additionally, gRPC's
  default `SO_REUSEPORT` let a second instance silently share an occupied port instead of failing.

## 2. Implementation

### `src/currency/src/server.cpp`
- **Race-safe signal handling**: `SIGTERM`/`SIGINT` are blocked with `sigprocmask` in `main`
  *before* any worker threads (gRPC, SDK exporters) are created, and delivered through
  `signalfd(2)` (`sys/signalfd.h`, Linux). No async-signal-unsafe work happens in a handler; the
  main thread blocks on `read(signal_fd, ...)`.
- **Bounded overall shutdown deadline** (`CURRENCY_SHUTDOWN_TIMEOUT_SECONDS`, default 10s): the
  deadline starts exactly when the termination signal is received and covers both the gRPC drain
  and every telemetry flush/shutdown step.
- **Bounded server drain**: on signal, `server->Shutdown(gpr_time_from_millis(drain_ms,
  GPR_CLOCK_MONOTONIC))` + `Wait()`, with `drain_ms = min(remaining/2, timeout/2)` so the drain can
  never consume the budget needed for telemetry shutdown.
- **Bounded telemetry shutdown**: tracer, meter and logger providers are flushed (`ForceFlush`)
  then shut down (`Shutdown`), each step wrapped in a `std::async`/`wait_for(remaining deadline)`
  helper so a hung exporter cannot exceed the overall deadline. Each step is logged with the
  remaining budget (`currency.shutdown.step` / `currency.shutdown.step_done` events).
- **Startup failures exit nonzero without waiting for a signal**: `BuildAndStart` returning null
  logs `currency.server.start_failed` and `main` returns 1 immediately; a missing port argument
  also returns 1. `GRPC_ARG_ALLOW_REUSEPORT=0` makes an occupied port a real startup failure.
- Concrete SDK providers are retained (`std::shared_ptr<sdk::trace::TracerProvider>`,
  `sdk::metrics::MeterProvider`, `sdk::logs::LoggerProvider`) for shutdown while the API-level
  globals keep pointing at the same objects.

### `src/currency/src/tracer_common.h`, `meter_common.h`, `logger_common.h`
- `initTracer()`, `initMeter()`, `initLogger()` now return the concrete SDK providers (previously
  `void`), keeping provider initialization code and shutdown capability together.

### `src/currency/CMakeLists.txt`
- `find_package(Threads REQUIRED)` and `Threads::Threads` linked (needed for `std::async`).

### `src/currency/Dockerfile`
- Entrypoint changed to `sh -c "exec /usr/local/bin/currency ${CURRENCY_PORT}"`: `exec` replaces
  the shell with the service process (PID 1), so container termination signals reach the service
  directly.

## 3. Verification (actual commands/outcomes via the benchmark tools)

1. `redeploy_currency` — build succeeded (only pre-existing semconv deprecation warnings), image
   `agent-forgetting/otel-currency:checkout-noise-01` deployed, container **Healthy**.
   - Two intermediate build failures were fixed during development (nostd/shared_ptr conversion for
     `SetTracerProvider`); final build is clean.
2. `probe_currency_shutdown` (final candidate):
   - `SIGTERM stop elapsed 0.4s`, container stop `exit code 0`.
   - `Exit status: 0`.
   - Final logs: `Received signal 15, initiating graceful shutdown` →
     `Currency service shutdown complete` (ordered tracer/meter/logger flush and shutdown steps
     completed within the deadline; a benign SDK warning
     `[MeterContext::Shutdown] Shutdown can be invoked only once` appears because the periodic
     metric reader's shutdown path also shuts down the meter context — it is logged and ignored by
     the SDK, and the process still exits 0 within 0.4s).
   - Restart: container **Healthy** again.
   - SIGINT shares the identical signalfd wait path (both signals blocked and multiplexed on the
     same descriptor), so the same graceful path applies.
3. Occupied-port startup-failure probe (focused test, entrypoint temporarily set to run a second
   instance against the already-bound `$CURRENCY_PORT`):
   - With gRPC's default `SO_REUSEPORT`, the second instance silently shared the port (container
     stayed healthy) — which motivated `GRPC_ARG_ALLOW_REUSEPORT=0`.
   - With reuseport disabled: `redeploy_currency` deploy step failed with
     `container currency is unhealthy` after only **3.3s** (foreground instance failed to bind,
     exited nonzero promptly without waiting for any termination signal; `BuildAndStart` returned
     null → exit 1). The temporary probe entrypoint was then reverted and the final candidate
     redeployed healthy (step 2 above).
4. The service remains functional after restart (deploy health check passes via the gRPC health
   service).

## 4. Limitations

- `docker stop`'s 10s SIGKILL backstop and `CURRENCY_SHUTDOWN_TIMEOUT_SECONDS` (default 10s,
  drain capped at 5s) leave the same budget for telemetry; a slow OTLP collector could truncate the
  final flush, which the bounded steps handle by timing out and logging.
- The benign `[MeterContext::Shutdown] Shutdown can be invoked only once` SDK warning is logged
  during meter shutdown (opentelemetry-cpp 1.27 reader/provider shutdown overlap); it does not
  affect the exit status or the deadline.
- SIGINT was verified by source inspection of the shared signalfd path; the benchmark probe only
  exercises SIGTERM.

---

# Benchmark Report — Checkout Failure Diagnosis and Frontend Fix (previous task, preserved)

Origin: http://127.0.0.1:18080 (OpenTelemetry Astronomy Shop, controlled fault conditions)

## 1. Diagnosis (before any code change)

### Failing request
`POST /api/checkout?currencyCode=USD` returned **HTTP 500 "Internal Server Error"** (plain-text,
non-JSON body). The browser received an unhandled error: console showed
`SyntaxError: Unexpected token 'I', "Internal S"... is not valid JSON` from `JSON.parse` inside the
frontend's shared request helper (`src/frontend/utils/Request.ts`), which blindly parsed any
non-empty response body and never checked `response.ok`.

### Services involved (observed live, before the fix)
- **frontend (Next.js API route `/api/checkout`)** — gRPC `PlaceOrder` call failed, error rethrown
  by `InstrumentationMiddleware`, producing Next.js default plain-text 500.
- **checkout** — `oteldemo.CheckoutService/PlaceOrder` failed with
  `could not charge the card: rpc error: code = Unknown desc = Payment request failed. Invalid token`
  (wrapped in `src/checkout/main.go chargeCard`).
- **payment** — `oteldemo.PaymentService/Charge` returned gRPC `UNKNOWN` with
  `Payment request failed. Invalid token. demo.user_context.loyalty_level=gold`, logged by the
  payment service at `charge.js:47:15` ("Payment request failed: Invalid token"). The
  `paymentFailure` feature flag was **on** in the live flagd UI (`/feature`); `paymentUnreachable`,
  `cartFailure` were **off**.
- **frontend-proxy (Envoy)** — reported HTTP 500 for the ingress request.

### Other active anomalies (observed at the same time, unrelated to checkout)
- `productCatalogFailure` flag targeted product `OLJCESPC7Z` → `/api/products/OLJCESPC7Z` and
  `/api/recommendations` returned 500 ("13 INTERNAL: Error: Product Catalog Fail Feature Flag
  Enabled"), e.g. trace `378b9d2684f17ea2028fdbfa5b923ad6`.
- `ad` service intermittent `14 UNAVAILABLE` on `GetAds` → `/api/data` 500 (trace
  `30ff206b400d63355042ba0546c1edfa`).
- The OpenSearch exporter in the collector dropped some payment error logs
  (`failed to parse field [attributes.err]` — malformed error attribute), so log-based evidence for
  the payment failure was partially lost; traces still carried the failure.

### Trace evidence for the original failure
Jaeger (via Grafana proxy `/grafana/api/datasources/proxy/uid/webstore-traces`) showed traces where:
- `frontend` span `executing api route (pages) /api/checkout` had `http.status_code=500`,
  `otel.status_code=ERROR`, exception `13 INTERNAL: ... could not charge the card ...`;
- `checkout` span `PlaceOrder` had `otel.status_description="failed to charge card: could not
  charge the card: ... Payment request failed. Invalid token"` (error=true);
- `payment` span `Charge` had `otel.status_description="Payment request failed. Invalid token"`.

## 2. Code changes (frontend only)

### `src/frontend/pages/api/checkout.ts`
- Wrapped the POST handler in try/catch; no longer rethrows to Next's generic 500.
- Payment-charge failures are detected by the checkout service's own client-safe marker
  (`could not charge the card`, from `src/checkout/main.go`) and return
  **HTTP 422** with `{ error: "Payment failed: the payment method was declined.", code: "PAYMENT_FAILED" }`.
  No upstream gRPC message, stack or flag details are leaked.
- All other failures return **HTTP 500** with `{ error: "Internal server error" }` — generic,
  no `PAYMENT_FAILED` code, no leaked upstream details.
- Both paths call `span.recordException()` and `span.setStatus(ERROR)` on the active span so the
  failure stays visible in traces.

### `src/frontend/utils/Request.ts` (shared request helper)
- Rejects non-success responses (`throw` instead of returning parsed text).
- Preserves a JSON server `error` message when the body is JSON.
- Safely handles non-JSON error bodies (plain text "Internal Server Error", "Bad Gateway") via
  try/catch around `JSON.parse`, falling back to `Request failed with status <code>`.
  This removes the browser-side `SyntaxError ... is not valid JSON` crash.

### `src/frontend/cypress/e2e/CheckoutErrorContract.cy.ts` (new focused regression spec)
1. Live end-to-end: add to cart → Place Order → asserts `POST /api/checkout` returns **422** with
   non-empty client-safe `error` and `code: PAYMENT_FAILED` (no leaked details).
2. Live API: malformed order payload → asserts **500** with body exactly
   `{ error: 'Internal server error' }`, no `PAYMENT_FAILED`, no leaked details.
3. Helper safety: intercepted non-JSON 502 body ("Bad Gateway") → asserts no uncaught
   `is not valid JSON` SyntaxError occurs in the app.

## 3. Verification (actual outcomes)

- **Redeploy** (`redeploy_frontend`): frontend Docker build + deploy succeeded, container healthy.
- **Live browser behavior (after fix)**: add item → Place Order →
  `POST /api/checkout?currencyCode=USD => 422 Unprocessable Entity` (observed in network log).
  Console now shows `Error: Payment failed: the payment method was declined.` — the server JSON
  message preserved by the helper — and **no JSON.parse SyntaxError**.
- **Live trace after fix** (Jaeger trace `c682f13d53c6eae89a08f06575939126`):
  - `payment` `oteldemo.PaymentService/Charge`: `otel.status_description="Payment request failed.
    Invalid token. demo.user_context.loyalty_level=gold"`, error=true;
  - `checkout` `PlaceOrder`: error with "could not charge the card: ...";
  - `frontend` `POST /api/checkout` and `/api/checkout` API-route spans: `http.status_code=422`,
    recorded exception;
  - `frontend-proxy` ingress/egress spans: HTTP `422`.
  Failure telemetry is preserved (frontend still records the exception on the span).
- **Cypress**: `src/frontend/cypress/e2e/CheckoutErrorContract.cy.ts` — **3/3 passing**
  (run_frontend_tests exit code 0).
- **Existing specs**: `Home.cy.ts` and `ProductDetail.cy.ts` — 4/6 passing. The two failures
  ("should recover corrupted stored session", "should not render product picture ... picture is
  missing") are unrelated to checkout/Request/checkout-API changes (session localStorage parsing and
  product image rendering; neither code path was modified) and predate the fix. The pre-existing
  `Checkout.cy.ts` "create an order with two items" happy path cannot pass while the live
  `paymentFailure` fault is active (the payment decline is the intended live behavior); the new
  spec asserts the 422 contract instead.

## 4. Limitations
- Payment-decline detection relies on the checkout service's own wrapper message
  "could not charge the card". A gRPC error-code or structured-flag based classification would be
  more robust but would require changes to the protected checkout service.
- `paymentUnreachable` (off in this environment) also surfaces through the same checkout wrapper and
  would currently be classified as a payment decline (422) rather than a 500.
- The collector's OpenSearch log exporter dropped some payment error logs due to a malformed
  `attributes.err` field; trace evidence was unaffected. Collector config was not modified
  (out of scope / protected).
- Two unrelated, pre-existing Cypress failures in `Home.cy.ts` / `ProductDetail.cy.ts` remain
  (session-recovery race and missing-image assertion), untouched by this change.

No Docker, Compose, environment, feature-flag or unrelated service files were modified.
