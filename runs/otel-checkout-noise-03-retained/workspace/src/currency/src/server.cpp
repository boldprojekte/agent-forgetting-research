// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

#include <cstdlib>
#include <iostream>
#include <math.h>
#include <demo.grpc.pb.h>
#include <grpc/health/v1/health.grpc.pb.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <future>
#include <memory>
#include <string>
#include <sys/signalfd.h>
#include <thread>
#include <unistd.h>

#include <grpc/support/time.h>

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

// Overall shutdown deadline. It starts when the termination signal is received
// and covers both the gRPC server drain and every telemetry flush/shutdown.
std::chrono::steady_clock::time_point shutdown_deadline;
long max_graceful_shutdown_ms;

namespace
{
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

bool RunServer(uint16_t port, int signal_fd)
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
  // Fail fast on an occupied port instead of silently sharing it via
  // SO_REUSEPORT; BuildAndStart then returns nullptr and we exit nonzero.
  builder.AddChannelArgument(GRPC_ARG_ALLOW_REUSEPORT, 0);

  std::unique_ptr<Server> server(builder.BuildAndStart());
  if (server == nullptr) {
    // Startup failure: exit nonzero immediately instead of waiting for a
    // termination signal (e.g. port already in use, invalid address).
    std::cerr << "currency server failed to start on " << address << std::endl;
    logger->Error(eventName("currency.server.start_failed"), "Currency Server failed to start");
    return false;
  }
  logger->Info(eventName("currency.server.started"),
               "Currency Server started",
               opentelemetry::common::MakeAttributes({{"server.address", address.c_str()}}));

  // Block here on the signal file descriptor until SIGTERM or SIGINT is
  // delivered. Signals are blocked process-wide before the gRPC server is
  // created, so no handler runs on an arbitrary service thread; delivery is
  // explicit and race-free via signalfd(2).
  signalfd_siginfo siginfo{};
  while (true) {
    ssize_t n = read(signal_fd, &siginfo, sizeof(siginfo));
    if (n == static_cast<ssize_t>(sizeof(siginfo))) {
      break;
    }
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) {
      continue;
    }
    // Unexpected signalfd failure: fall back to waiting on the server so the
    // process does not busy-loop; shutdown will happen on signal delivery.
    server->Wait();
    server->Shutdown();
    return true;
  }

  std::cout << "Received signal " << siginfo.ssi_signo << ", initiating graceful shutdown"
            << std::endl;
  logger->Info(eventName("currency.server.shutdown_started"), "Graceful shutdown started");

  // The overall shutdown deadline starts exactly when the termination signal
  // is received and covers the gRPC drain below plus every telemetry
  // flush/shutdown step performed by main() after this function returns.
  shutdown_deadline = std::chrono::steady_clock::now() +
                      std::chrono::milliseconds(max_graceful_shutdown_ms);

  // Stop accepting new work and give in-flight RPCs a bounded opportunity to
  // finish. The drain must leave time for the telemetry shutdown below, so it
  // is capped at half of the remaining overall deadline.
  auto now = std::chrono::steady_clock::now();
  auto remaining_ms =
      std::chrono::duration_cast<std::chrono::milliseconds>(shutdown_deadline - now).count();
  if (remaining_ms < 0) {
    remaining_ms = 0;
  }
  auto drain_ms = std::min<long long>(remaining_ms / 2, max_graceful_shutdown_ms / 2);
  if (drain_ms < 0) {
    drain_ms = 0;
  }
  logger->Info(eventName("currency.server.shutdown_drain"),
               "Draining gRPC server",
               opentelemetry::common::MakeAttributes(
                   {{"shutdown.drain_ms", static_cast<int64_t>(drain_ms)}}));
  server->Shutdown(
      gpr_time_from_millis(static_cast<int64_t>(drain_ms), GPR_CLOCK_MONOTONIC));
  server->Wait();
  logger->Info(eventName("currency.server.shutdown_drained"), "gRPC server drained");
  return true;
}
} // namespace

int main(int argc, char **argv) {

  if (argc < 2) {
    std::cerr << "Usage: currency <port>" << std::endl;
    return 1;
  }

  uint16_t port = atoi(argv[1]);

  const char* shutdown_seconds_env = std::getenv("CURRENCY_SHUTDOWN_TIMEOUT_SECONDS");
  long shutdown_seconds = shutdown_seconds_env != nullptr ? atol(shutdown_seconds_env) : 10;
  if (shutdown_seconds <= 0) {
    shutdown_seconds = 10;
  }
  max_graceful_shutdown_ms = shutdown_seconds * 1000;

  // Block SIGTERM/SIGINT before any worker threads (gRPC, SDK exporters) are
  // created so the disposition is inherited process-wide and delivery goes
  // through the signal file descriptor instead of a signal handler that could
  // race with arbitrary threads.
  sigset_t signal_mask;
  sigemptyset(&signal_mask);
  sigaddset(&signal_mask, SIGTERM);
  sigaddset(&signal_mask, SIGINT);
  if (sigprocmask(SIG_BLOCK, &signal_mask, nullptr) != 0) {
    std::cerr << "Failed to block termination signals" << std::endl;
    return 1;
  }
  int signal_fd = signalfd(-1, &signal_mask, 0);
  if (signal_fd < 0) {
    std::cerr << "Failed to create signalfd" << std::endl;
    return 1;
  }

  auto tracer_provider  = initTracer();
  auto meter_provider   = initMeter();
  auto logger_provider  = initLogger();
  currency_counter = initIntCounter("demo.exchange.conversions", version);
  logger = getLogger(name);

  if (!RunServer(port, signal_fd)) {
    // Startup failure: exit nonzero without waiting for a termination signal.
    close(signal_fd);
    return 1;
  }

  // The overall shutdown deadline started when the signal was received; flush
  // and shut down each telemetry provider within what remains of it.
  auto bounded = [](const char* what, auto&& operation) {
    auto future = std::async(std::launch::async, std::forward<decltype(operation)>(operation));
    auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
        shutdown_deadline - std::chrono::steady_clock::now());
    if (remaining.count() < 0) {
      remaining = std::chrono::milliseconds(0);
    }
    logger->Info(eventName("currency.shutdown.step"),
                 "Telemetry shutdown step",
                 opentelemetry::common::MakeAttributes(
                     {{"shutdown.step", what},
                      {"shutdown.remaining_ms", static_cast<int64_t>(remaining.count())}}));
    if (future.wait_for(remaining) == std::future_status::ready) {
      logger->Info(eventName("currency.shutdown.step_done"), "Telemetry shutdown step done",
                   opentelemetry::common::MakeAttributes({{"shutdown.step", what}}));
      return true;
    }
    std::cerr << "Telemetry shutdown step timed out: " << what << std::endl;
    logger->Error(eventName("currency.shutdown.step_timeout"), "Telemetry shutdown step timed out",
                  opentelemetry::common::MakeAttributes({{"shutdown.step", what}}));
    return false;
  };

  bounded("tracer_flush", [&] { tracer_provider->ForceFlush(); });
  bounded("tracer_shutdown", [&] { tracer_provider->Shutdown(); });
  bounded("meter_flush", [&] { meter_provider->ForceFlush(); });
  bounded("meter_shutdown", [&] { meter_provider->Shutdown(); });
  bounded("logger_flush", [&] { logger_provider->ForceFlush(); });
  bounded("logger_shutdown", [&] { logger_provider->Shutdown(); });

  logger->Info(eventName("currency.server.shutdown_complete"), "Graceful shutdown complete");
  std::cout << "Currency service shutdown complete" << std::endl;

  close(signal_fd);
  return 0;
}
