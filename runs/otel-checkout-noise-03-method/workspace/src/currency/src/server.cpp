// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

#include <cstdlib>
#include <iostream>
#include <math.h>
#include <demo.grpc.pb.h>
#include <grpc/health/v1/health.grpc.pb.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <memory>
#include <mutex>
#include <thread>

#include <pthread.h>

#include "opentelemetry/trace/context.h"
#include "opentelemetry/semconv/incubating/rpc_attributes.h"
#include "opentelemetry/trace/span_context_kv_iterable_view.h"
#include "opentelemetry/baggage/baggage.h"
#include "opentelemetry/nostd/string_view.h"
#include "opentelemetry/logs/event_id.h"
#include "logger_common.h"
#include "meter_common.h"
#include "tracer_common.h"

#include <grpcpp/grpcpp.h>
#include <grpcpp/server.h>
#include <grpcpp/server_builder.h>
#include <grpcpp/server_context.h>
#include <grpcpp/impl/codegen/string_ref.h>
#include <grpc/support/time.h>

using namespace std;
using namespace opentelemetry::baggage;
using namespace opentelemetry::trace;

using oteldemo::Empty;
using oteldemo::GetSupportedCurrenciesResponse;
using oteldemo::CurrencyConversionRequest;
using oteldemo::Money;

using grpc::Status;
using grpc::ServerContext;
using grpc::ServerBuilder;
using grpc::Server;

using Span            = Span;
using SpanContext     = SpanContext;
namespace context     = opentelemetry::context;
namespace metrics_api = opentelemetry::metrics;
namespace nostd       = opentelemetry::nostd;
namespace semconv     = opentelemetry::semconv;

using opentelemetry::logs::EventId;

namespace
{
  // Overall graceful shutdown budget. It starts when SIGTERM/SIGINT is received
  // and covers both the gRPC server drain and every telemetry flush/shutdown.
  constexpr std::chrono::seconds kShutdownDeadline{10};

  std::mutex server_mutex;
  Server* server_ptr = nullptr;

  std::mutex signal_mutex;
  std::condition_variable signal_cv;
  std::atomic<bool> signal_received{false};
  std::chrono::steady_clock::time_point signal_time;

  std::chrono::steady_clock::time_point GetSignalTime()
  {
    std::lock_guard<std::mutex> lock(signal_mutex);
    return signal_time;
  }

  // Remaining shutdown budget since the termination signal was received.
  std::chrono::microseconds RemainingShutdownBudget()
  {
    auto deadline = GetSignalTime() + kShutdownDeadline;
    auto now      = std::chrono::steady_clock::now();
    if (now >= deadline)
    {
      return std::chrono::microseconds::zero();
    }
    return std::chrono::duration_cast<std::chrono::microseconds>(deadline - now);
  }

  // Blocks on sigwait (async-signal-safe) for SIGTERM/SIGINT. All shutdown work
  // happens on this normal thread, never inside a signal handler. The gRPC
  // server is shut down with a deadline so in-flight work gets a bounded
  // opportunity to finish within the overall shutdown budget.
  void SignalWaitThread()
  {
    sigset_t set;
    sigemptyset(&set);
    sigaddset(&set, SIGTERM);
    sigaddset(&set, SIGINT);

    int sig = 0;
    if (sigwait(&set, &sig) != 0)
    {
      return;
    }

    {
      std::lock_guard<std::mutex> lock(signal_mutex);
      signal_time     = std::chrono::steady_clock::now();
      signal_received = true;
    }

    std::cerr << "termination signal " << sig << " received, starting graceful shutdown (deadline "
              << std::chrono::duration_cast<std::chrono::milliseconds>(kShutdownDeadline).count()
              << "ms)\n"
              << std::flush;

    {
      std::lock_guard<std::mutex> lock(server_mutex);
      if (server_ptr != nullptr)
      {
        // Stop accepting new work and drain in-flight RPCs with a deadline:
        // remaining RPCs are cancelled when the deadline expires.
        gpr_timespec now    = gpr_now(GPR_CLOCK_MONOTONIC);
        gpr_timespec delta  = gpr_time_from_millis(
            std::chrono::duration_cast<std::chrono::milliseconds>(kShutdownDeadline).count(),
            GPR_TIMESPAN);
        server_ptr->Shutdown(gpr_time_add(now, delta));
      }
    }

    signal_cv.notify_all();
  }

  EventId eventName(nostd::string_view name) {
    // The OTLP exporter ignores the numeric EventId and exports only the event name.
    // Use 0 to satisfy the C++ API; revisit when the C++ SDK provides guidance.
    return EventId{0, name};
  }

  std::unordered_map<std::string, double> currency_conversion
  {
    {"EUR", 1.0},
    {"USD", 1.1305},
    {"JPY", 126.40},
    {"BGN", 1.9558},
    {"CZK", 25.592},
    {"DKK", 7.4609},
    {"GBP", 0.85970},
    {"HUF", 315.51},
    {"PLN", 4.2996},
    {"RON", 4.7463},
    {"SEK", 10.5375},
    {"CHF", 1.1360},
    {"ISK", 136.80},
    {"NOK", 9.8040},
    {"HRK", 7.4210},
    {"RUB", 74.4208},
    {"TRY", 6.1247},
    {"AUD", 1.6072},
    {"BRL", 4.2682},
    {"CAD", 1.5128},
    {"CNY", 7.5857},
    {"HKD", 8.8743},
    {"IDR", 15999.40},
    {"ILS", 4.0875},
    {"INR", 79.4320},
    {"KRW", 1275.05},
    {"MXN", 21.7999},
    {"MYR", 4.6289},
    {"NZD", 1.6679},
    {"PHP", 59.083},
    {"SGD", 1.5349},
    {"THB", 36.012},
    {"ZAR", 16.0583},
  };

  const char* version_env = std::getenv("VERSION");
  std::string version = version_env != nullptr ? version_env : "unknown";
  std::string name{ "currency" };

  nostd::unique_ptr<metrics_api::Counter<uint64_t>> currency_counter;
  nostd::shared_ptr<opentelemetry::logs::Logger> logger;

class HealthServer final : public grpc::health::v1::Health::Service
{
  Status Check(
    ServerContext* context,
    const grpc::health::v1::HealthCheckRequest* request,
    grpc::health::v1::HealthCheckResponse* response) override
  {
    response->set_status(grpc::health::v1::HealthCheckResponse::SERVING);
    return Status::OK;
  }
};

class CurrencyService final : public oteldemo::CurrencyService::Service
{
  Status GetSupportedCurrencies(ServerContext* context,
  	const Empty* request,
  	GetSupportedCurrenciesResponse* response) override
  {
    StartSpanOptions options;
    options.kind = SpanKind::kServer;
    GrpcServerCarrier carrier(context);

    auto prop        = context::propagation::GlobalTextMapPropagator::GetGlobalPropagator();
    auto current_ctx = context::RuntimeContext::GetCurrent();
    auto new_context = prop->Extract(carrier, current_ctx);
    options.parent   = GetSpan(new_context)->GetContext();

    std::string span_name = "Currency/GetSupportedCurrencies";
    auto span =
        get_tracer("currency")->StartSpan(span_name,
                                      {{semconv::rpc::kRpcSystem, "grpc"},
                                       {semconv::rpc::kRpcService, "oteldemo.CurrencyService"},
                                       {semconv::rpc::kRpcMethod, "GetSupportedCurrencies"},
                                       {semconv::rpc::kRpcGrpcStatusCode, semconv::rpc::RpcGrpcStatusCodeValues::kOk}},
                                      options);
    auto scope = get_tracer("currency")->WithActiveSpan(span);

    span->AddEvent("Processing supported currencies request");

    for (auto &code : currency_conversion) {
      response->add_currency_codes(code.first);
    }

    span->AddEvent("Currencies fetched, response sent back");
    span->SetStatus(StatusCode::kOk);

    logger->Info(eventName("currency.get_supported_currencies"), "GetSupportedCurrencies successful");

    // Make sure to end your spans!
    span->End();
  	return Status::OK;
  }

  double getDouble(Money& money) {
    auto units = money.units();
    auto nanos = money.nanos();

    double decimal = 0.0;
    while (nanos != 0) {
      double t = (double)(nanos%10)/10;
      nanos = nanos/10;
      decimal = decimal/10 + t;
    }

    return double(units) + decimal;
  }

  void getUnitsAndNanos(Money& money, double value) {
    long unit = (long)value;
    double rem = value - unit;
    long nano = rem * pow(10, 9);
    money.set_units(unit);
    money.set_nanos(nano);
  }

  Status Convert(ServerContext* context,
  	const CurrencyConversionRequest* request,
  	Money* response) override
  {
    StartSpanOptions options;
    options.kind = SpanKind::kServer;
    GrpcServerCarrier carrier(context);

    auto prop        = context::propagation::GlobalTextMapPropagator::GetGlobalPropagator();
    auto current_ctx = context::RuntimeContext::GetCurrent();
    auto new_context = prop->Extract(carrier, current_ctx);
    options.parent   = GetSpan(new_context)->GetContext();

    std::string span_name = "Currency/Convert";
    auto span =
        get_tracer("currency")->StartSpan(span_name,
                                      {{semconv::rpc::kRpcSystem, "grpc"},
                                       {semconv::rpc::kRpcService, "oteldemo.CurrencyService"},
                                       {semconv::rpc::kRpcMethod, "Convert"},
                                       {semconv::rpc::kRpcGrpcStatusCode, semconv::rpc::RpcGrpcStatusCodeValues::kOk}},
                                      options);
    auto scope = get_tracer("currency")->WithActiveSpan(span);

    span->AddEvent("Processing currency conversion request");

    try {
      // Do the conversion work
      Money from = request->from();
      string from_code = from.currency_code();
      double rate = currency_conversion[from_code];
      double one_euro = getDouble(from) / rate ;

      string to_code = request->to_code();
      double to_rate = currency_conversion[to_code];

      double final = one_euro * to_rate;
      getUnitsAndNanos(*response, final);
      response->set_currency_code(to_code);

      span->SetAttribute("demo.exchange.from", from_code);
      span->SetAttribute("demo.exchange.to", to_code);

      CurrencyCounter(to_code);

      span->AddEvent("Conversion successful, response sent back");
      span->SetStatus(StatusCode::kOk);

      logger->Info(eventName("currency.conversion"),
                   "conversion successful",
                   opentelemetry::common::MakeAttributes(
                       {{"currency.from", from_code.c_str()},
                        {"currency.to", to_code.c_str()}}));
      
      // End the span
      span->End();
      return Status::OK;

    } catch(...) {
      span->AddEvent("Conversion failed");
      span->SetStatus(StatusCode::kError);

      logger->Error(eventName("currency.conversion_failed"), "conversion failure");

      span->End();
      return Status::CANCELLED;
    }
    return Status::OK;
  }

  void CurrencyCounter(const std::string& to_code)
  {
      std::map<std::string, std::string> labels = { {"demo.exchange.to", to_code} };
      auto labelkv = common::KeyValueIterableView<decltype(labels)>{ labels };
      currency_counter->Add(1, labelkv);
  }
};

void RunServer(uint16_t port)
{
  std::string ip("0.0.0.0");

  const char* ipv6_enabled = std::getenv("IPV6_ENABLED");

  if (ipv6_enabled != nullptr && std::string(ipv6_enabled) == "true") {
    ip = "[::]";
    logger->Info(eventName("currency.server.ip_overwrite"),
                 "Overwriting Localhost IP",
                 opentelemetry::common::MakeAttributes({{"server.address", ip.c_str()}}));
  }

  std::string address(ip + ":" +  std::to_string(port));

  CurrencyService currencyService;
  HealthServer healthService;
  ServerBuilder builder;

  builder.RegisterService(&currencyService);
  builder.RegisterService(&healthService);
  builder.AddListeningPort(address, grpc::InsecureServerCredentials());
  // Fail fast when the port is already bound by another instance instead of
  // silently sharing it via SO_REUSEPORT (gRPC's default on Linux).
  builder.AddChannelArgument(GRPC_ARG_ALLOW_REUSEPORT, 0);

  std::unique_ptr<Server> server(builder.BuildAndStart());
  if (server == nullptr)
  {
    // Startup failure: report (including to telemetry) and let main exit
    // nonzero promptly without waiting for a termination signal.
    std::cerr << "Currency Server failed to start on " << address << "\n" << std::flush;
    logger->Error(eventName("currency.server.start_failed"),
                  "Currency Server failed to start",
                  opentelemetry::common::MakeAttributes({{"server.address", address.c_str()}}));
    return;
  }

  {
    std::lock_guard<std::mutex> lock(server_mutex);
    server_ptr = server.get();
  }

  logger->Info(eventName("currency.server.started"),
               "Currency Server started",
               opentelemetry::common::MakeAttributes({{"server.address", address.c_str()}}));

  // Returns once the signal thread initiated shutdown (new work is no longer
  // accepted) and the bounded drain completed or expired.
  server->Wait();

  {
    std::lock_guard<std::mutex> lock(server_mutex);
    server_ptr = nullptr;
  }

  std::cout << "gRPC server shut down\n" << std::flush;
}
}

int main(int argc, char **argv) {

  if (argc < 2) {
    std::cerr << "Usage: currency <port>";
    return EXIT_FAILURE;
  }

  uint16_t port = atoi(argv[1]);

  // Block SIGTERM/SIGINT in this thread (and every thread created afterwards,
  // including gRPC workers) so a dedicated thread can consume them via sigwait.
  // No signal handler performs shutdown work, which keeps termination
  // race-safe and async-signal-safe.
  sigset_t shutdown_signals;
  sigemptyset(&shutdown_signals);
  sigaddset(&shutdown_signals, SIGTERM);
  sigaddset(&shutdown_signals, SIGINT);
  if (pthread_sigmask(SIG_BLOCK, &shutdown_signals, nullptr) != 0) {
    std::cerr << "failed to block termination signals\n" << std::flush;
    return EXIT_FAILURE;
  }

  std::thread signal_thread(SignalWaitThread);

  initTracer();
  initMeter();
  initLogger();
  currency_counter = initIntCounter("demo.exchange.conversions", version);
  logger = getLogger(name);
  RunServer(port);

  const bool terminated_by_signal = signal_received.load();

  // Fixed teardown order, used for both paths. Instruments and logger handles
  // are released first (while their providers are alive), then each provider is
  // flushed and shut down, then the API provider singletons are cleared and the
  // concrete providers destroyed. All of this happens inside main, within the
  // remaining overall shutdown budget, so nothing is left to race gRPC/abseil
  // static teardown after main returns.
  std::chrono::microseconds fixed_budget = std::chrono::microseconds::zero();
  auto remaining_shutdown_budget = [&fixed_budget]() {
    return fixed_budget.count() > 0 ? fixed_budget : RemainingShutdownBudget();
  };
  const auto release_all = [&]() {
    // Release instrument and logger handles first, while their providers are
    // still alive.
    currency_counter = nullptr;
    logger           = nullptr;

    shutdownTracer(remaining_shutdown_budget());
    opentelemetry::trace::Provider::SetTracerProvider(
        nostd::shared_ptr<opentelemetry::trace::TracerProvider>());
    tracer_provider = nullptr;

    shutdownMeter(remaining_shutdown_budget());
    metrics_api::Provider::SetMeterProvider(nostd::shared_ptr<metrics_api::MeterProvider>());
    // Intentionally leak the shut-down meter provider: destroying it re-enters
    // MeterContext::Shutdown from the reader destructor and crashes the OTLP
    // gRPC metric exporter teardown in SDK 1.27. Shutdown has already
    // completed, so no telemetry state is left unflushed.
    (void)new nostd::shared_ptr<metric_sdk::MeterProvider>(meter_provider);
    meter_provider = nostd::shared_ptr<metric_sdk::MeterProvider>();

    shutdownLogger(remaining_shutdown_budget());
    opentelemetry::logs::Provider::SetLoggerProvider(
        nostd::shared_ptr<opentelemetry::logs::LoggerProvider>());
    logger_provider = nullptr;
  };

  if (!terminated_by_signal) {
    // Startup failure (or unexpected server exit): tear down telemetry with a
    // small fixed budget and exit nonzero promptly, without waiting for a
    // termination signal.
    std::cerr << "server stopped without receiving a termination signal\n" << std::flush;
    fixed_budget = std::chrono::seconds(10);
    release_all();
    signal_thread.join();
    return EXIT_FAILURE;
  }

  // Flush and shut down the telemetry providers within the remaining overall
  // shutdown budget (which started when the signal was received and includes
  // the gRPC server drain above).
  auto budget = RemainingShutdownBudget();
  std::cout << "shutting down telemetry providers (remaining budget "
            << std::chrono::duration_cast<std::chrono::milliseconds>(budget).count() << "ms)\n"
            << std::flush;

  release_all();

  std::cout << "graceful shutdown complete\n" << std::flush;
  signal_thread.join();

  return EXIT_SUCCESS;
}
