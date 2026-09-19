# Benchmark Report

## Part 2 — Currency service graceful SIGTERM/SIGINT shutdown

### Problem

The C++ currency service relied on abrupt termination: the gRPC `Server::Wait()`/`Shutdown()` pair
ran without any signal handling, telemetry providers were never flushed or shut down, and the
container entrypoint (`sh -c "./usr/local/bin/currency ${CURRENCY_PORT}"`) did not reliably forward
termination signals to the service process.

### Implementation (files changed)

- `src/currency/src/server.cpp`
  - `main` blocks SIGTERM/SIGINT via `pthread_sigmask` before any thread (including gRPC workers)
    is created, so a dedicated signal thread can consume them with `sigwait` — no shutdown work
    happens inside a signal handler (race-safe, Linux containers).
  - A `SignalWaitThread` records the signal receipt time and calls
    `server->Shutdown(deadline)` with a deadline derived from the overall shutdown budget
    (`kShutdownDeadline = 10s`): the server stops accepting work immediately and in-flight RPCs get
    a bounded opportunity to finish before being cancelled.
  - After the drain, the remaining budget (`RemainingShutdownBudget()`) is used to flush and shut
    down the tracer, meter and logger providers **in that order**, all inside `main`, within the
    same overall deadline that started at signal receipt.
  - Startup failures (`BuildAndStart` returns null, or missing port argument) log to stderr, emit an
    OTLP error log (`currency.server.start_failed`), tear down telemetry with a fixed budget and
    `return EXIT_FAILURE` — they never wait for a termination signal. A missing-port invocation now
    exits 1 instead of 0.
  - gRPC's default `SO_REUSEPORT` silently shares an occupied port; `GRPC_ARG_ALLOW_REUSEPORT=0`
    makes a port conflict a real startup failure (fail fast).
  - Teardown ordering is fixed (instruments/logger handles → provider flush+shutdown → API provider
    singletons cleared → concrete providers destroyed/leaked) so nothing races gRPC/abseil static
    teardown after `main` returns. The shut-down meter provider handle is intentionally leaked: in
    SDK 1.27 destroying it re-enters `MeterContext::Shutdown` from the reader destructor and
    segfaults the OTLP gRPC metric exporter teardown (exit 139 observed before this change).
- `src/currency/src/tracer_common.h`, `meter_common.h`, `logger_common.h`
  - Retain the concrete SDK providers (`sdk::trace::TracerProvider`, `sdk::metrics::MeterProvider`,
    `sdk::logs::LoggerProvider`) in module-level handles behind mutexes and add
    `shutdownTracer/shutdownMeter/shutdownLogger(timeout)` performing bounded
    `ForceFlush(timeout)` + `Shutdown()`.
- `src/currency/Dockerfile`
  - Entrypoint is now exec-form signal-forwarding:
    `ENTRYPOINT ["/bin/sh", "-c", "exec ./usr/local/bin/currency ${CURRENCY_PORT}"]` — `exec`
    replaces the shell, so the currency process is PID 1 and receives SIGTERM/SIGINT directly.

### Verification — exact commands and outcomes (benchmark tools)

- `redeploy_currency` (build + replace container + health wait): **exit 0**; image
  `agent-forgetting/otel-currency:checkout-noise-01`, only benign deprecated `kRpc*` semconv
  warnings from pre-existing code. Repeated after the final code state — build and deploy healthy.
- `probe_currency_shutdown` (SIGTERM the live container, observe exit and final logs, restart) —
  final run on the production entrypoint:
  - `SIGTERM stop elapsed 0.3s`, container **exit status 0**, restart healthy.
  - Final logs show the full ordered graceful sequence:
    `termination signal 15 received, starting graceful shutdown (deadline 10000ms)` →
    `gRPC server shut down` → `tracer provider flushed` / `shut down` →
    `meter provider flushed` / `shut down` → `logger provider flushed` / `shut down` →
    `graceful shutdown complete`. Total well under the 10s deadline.
  - One benign `[Warning] MeterContext::Shutdown can be invoked only once` remains from the
    API-provider reference teardown chain; it is non-fatal (exit 0) and logged only.
- Startup-failure verification (occupied-port probe): during a temporary probe entrypoint that
  launched a second instance against the first instance's port, the live stack produced the
  OpenSearch log record `"Currency Server failed to start"` (observed at
  `2026-09-19T17:03:00.88Z` via `/grafana/api/datasources/proxy/uid/webstore-logs`, index
  `otel-logs-2026-09-19`) one second after the first instance's `Currency Server started`, and the
  failing instance exited promptly (the container stayed healthy, served by the first instance, no
  hang). Combined with the source path (`RunServer` failure branch → `EXIT_FAILURE`, no signal
  wait), this demonstrates prompt nonzero exit on startup failure. Note: `GRPC_ARG_ALLOW_REUSEPORT=0`
  was required for this — gRPC otherwise shares the port silently.
- Iteration history: three intermediate probes exited 139 (SIGSEGV) — in MeterProvider destruction
  (double shutdown re-entry) and in static teardown races; each was localized with flushed step
  markers and fixed (teardown reordering + intentional leak of the shut-down meter provider).
- No Compose, environment or feature-flag files were modified; flags were only read.

### Limitations

- The exact process exit code of the probe's second (port-conflicted) instance could not be read
  directly: the phase does not provide an exec/`docker logs` tool and rejects new test source files
  (the focused CTest probe was refused), so the occupied-port evidence is the observed
  startup-failure telemetry record plus prompt exit (container healthy, no hang) plus the
  source-determined `EXIT_FAILURE` path.
- The benign `MeterContext::Shutdown can be invoked only once` warning from SDK 1.27's teardown
  chain remains (exit status 0, logged only).

---

## Part 1 — Checkout failure contract fix (earlier report, preserved)


Origin: http://127.0.0.1:18080 (OpenTelemetry Astronomy Shop, controlled fault conditions active)

## 1. Diagnosis (before any code change)

**Reproduction (browser):** added a product to the cart and clicked *Place Order*. The user stayed
on `/cart` with no visible error and an unchanged cart. The network log showed
`POST /api/checkout?currencyCode=USD → 500 Internal Server Error` (unhandled, empty body rendered by
Next as `Internal Server Error`).

**Failing request / trace / services (Jaeger via `/grafana/api/datasources/proxy/uid/webstore-traces`):**

- Trace `fdf93287fae8fdf5c7f0d667b5ddf60c` (frontend `POST /api/checkout?currencyCode=USD`,
  `user.id = eae5ae84-ec94-4001-b4a0-fde75051a3f3`):
  - `frontend` — next.js span *executing api route /api/checkout*: HTTP 500,
    `exception.message = 13 INTERNAL: failed to charge card: could not charge the card: rpc error:
    code = Unknown desc = Payment request failed. Invalid token. demo.user_context.loyalty_level=gold`.
    The error propagated out of the unmodified handler, so the API returned a generic 500 with no
    JSON body.
  - `frontend` → `checkout` — `grpc.oteldemo.CheckoutService/PlaceOrder` (client + server spans,
    status 13 INTERNAL, `paymentUnreachable = off`).
  - `checkout` → `payment` — `oteldemo.PaymentService/Charge` → payment `charge` span ERROR at
    `/usr/src/app/charge.js:47`: `Payment request failed. Invalid token.` with attribute
    `demo.user_context.loyalty_level=gold` — the simulated `paymentFailure` flag decline.

**Correlated anomalies active at the same time (other traces):**

- `productCatalogFailure` flag ON → `product-catalog` returns *failed to get product "OLJCESPC7Z"*;
  checkout PlaceOrder fails with *failed to prepare order: failed to get product #OLJCESPC7Z*,
  and `GET /api/products/OLJCESPC7Z` and `GET /api/cart` (cart containing that product) return 500.
- Intermittent `GET /api/recommendations` 500s on the home page.
- The browser's own OTLP export to `localhost:8080/otlp-http` was refused from the host
  (telemetry verified through the Jaeger/Grafana surfaces instead).

**Root cause (frontend):** `pages/api/checkout.ts` never caught errors from
`CheckoutGateway.placeOrder`; the gRPC INTERNAL error bubbled through
`InstrumentationMiddleware`, producing an unhandled HTTP 500 with no client-safe body. The
payment decline (an expected client-facing failure) was indistinguishable from unrelated internal
failures. Client-side, `utils/Request.ts` parsed any response body as JSON without checking
`response.ok`, so error responses were never rejected.

## 2. Code changes (frontend only)

- `src/frontend/utils/Request.ts`
  - Rejects non-success responses with a new exported `RequestError` (`status`, `code`, `payload`).
  - Preserves a JSON server message from `{ error, code }` bodies; safely falls back to a generic
    `'Request failed'` message for non-JSON (HTML), empty or malformed bodies instead of throwing
    `JSON.parse` errors.
- `src/frontend/pages/api/checkout.ts`
  - Wraps `placeOrder` and the product enrichment in try/catch.
  - Payment declines (matched on the checkout service's charge-failure message) return
    **HTTP 422** with `{ error: "Payment failed. Please review your payment details and try again.",
    code: "PAYMENT_FAILED" }` — client-safe, no upstream details.
  - Unrelated internal failures return **HTTP 500** with `{ error: "Internal server error" }` — no
    `PAYMENT_FAILED` code, no leaked details.
  - Both paths record the exception, set span ERROR status and `http.status_code` on the active
    span, so the failure remains fully visible in traces/logs/metrics. The 200 success path is
    unchanged.
- `src/frontend/cypress/e2e/CheckoutFailure.cy.ts` (new focused regression spec).

No Docker, Compose, environment, feature-flag or unrelated service files were modified; flags were
only read, never edited or disabled.

## 3. Verification (actual outcomes)

- Redeployed via the benchmark tool: build + health check succeeded (twice, including after the
  final code state).
- Focused live Cypress spec `CheckoutFailure.cy.ts` against the redeployed stack — **3/3 passing**
  (final run, exit code 0):
  1. *returns 422 PAYMENT_FAILED for simulated payment declines* — real browser checkout, intercept
     captured `422` with `code: "PAYMENT_FAILED"` and non-empty client-safe `error`, no 500, no
     leaked upstream text.
  2. *keeps unrelated internal checkout failures as generic 500 JSON* — cart with the
     `productCatalogFailure`-affected product; `POST /api/checkout` returned `500` JSON with a
     non-empty `error`, no `PAYMENT_FAILED`, no upstream details.
  3. *handles non-JSON error bodies safely* — intercepted `GET /api/cart` returning a 502 HTML
     body; the helper rejected it safely (no `JSON.parse` crash) and the cart page still rendered.
- Trace evidence after the fix (Jaeger, lookback 15m): the browser session's checkout span now
  shows `http.status_code = 422` with the recorded exception (`failed to charge card ... Invalid
  token`), payment `charge` span still ERROR — the decline is still fully visible in telemetry, and
  load-generator checkout spans also show 422s where they previously showed 500s.
- Pre-existing specs (`Checkout.cy.ts`, `Home.cy.ts`, `ProductDetail.cy.ts`): 4/7 pass.
  Failures are environmental/pre-existing, not caused by this change:
  - `Checkout.cy.ts` fails before any checkout request (cart item count never reaches 2 — the
    active cart/product-catalog anomaly; this spec also cannot complete a successful order under a
    100% payment-decline fault).
  - `Home.cy.ts` "recover corrupted stored session" and `ProductDetail.cy.ts` picture rendering fail
    in this environment; session/image code was not touched.

## 4. Remaining limitations

- The controlled `paymentFailure` fault is currently at **100%** (20/20 consecutive declines in a
  dedicated success-path attempt), so a successful checkout (`200` + navigation to
  `/cart/checkout/[orderId]`) could not be observed live. The 200 success path is unchanged by the
  diff and the helper only adds behavior for `!response.ok`; a temporary success-path test was
  removed because it cannot pass under this fault.
- The cart page (`CartDetail.tsx`) does not render a user-facing error for a rejected `placeOrder`
  call (editing it is out of scope for this phase), so the declined checkout surfaces as an
  unhandled promise rejection in the browser; the HTTP contract, error body and telemetry are
  correct and covered by tests.
- Pre-existing spec failures listed above persist and are unrelated to this change.
