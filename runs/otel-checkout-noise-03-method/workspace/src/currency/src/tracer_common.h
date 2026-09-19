// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

#pragma once
#include "opentelemetry/exporters/otlp/otlp_grpc_exporter_factory.h"
#include "opentelemetry/context/propagation/global_propagator.h"
#include "opentelemetry/context/propagation/text_map_propagator.h"
#include "opentelemetry/exporters/ostream/span_exporter_factory.h"
#include "opentelemetry/nostd/shared_ptr.h"
#include "opentelemetry/sdk/trace/batch_span_processor_factory.h"
#include "opentelemetry/sdk/trace/tracer_context.h"
#include "opentelemetry/sdk/trace/tracer_context_factory.h"
#include "opentelemetry/sdk/trace/tracer_provider_factory.h"
#include "opentelemetry/trace/propagation/http_trace_context.h"
#include "opentelemetry/trace/provider.h"

#include <grpcpp/grpcpp.h>
#include <cstring>
#include <chrono>
#include <iostream>
#include <mutex>
#include <vector>

using grpc::ClientContext;
using grpc::ServerContext;

namespace
{
// Concrete SDK tracer provider, retained for explicit flush and shutdown.
std::shared_ptr<opentelemetry::sdk::trace::TracerProvider> tracer_provider;
std::mutex provider_shutdown_mutex;
class GrpcClientCarrier : public opentelemetry::context::propagation::TextMapCarrier
{
public:
  GrpcClientCarrier(ClientContext *context) : context_(context) {}
  GrpcClientCarrier() = default;
  virtual opentelemetry::nostd::string_view Get(
      opentelemetry::nostd::string_view key) const noexcept override
  {
    return "";
  }

  virtual void Set(opentelemetry::nostd::string_view key,
                   opentelemetry::nostd::string_view value) noexcept override
  {
    std::cout << " Client ::: Adding " << key << " " << value << "\n";
    context_->AddMetadata(key.data(), value.data());
  }

  ClientContext *context_;
};

class GrpcServerCarrier : public opentelemetry::context::propagation::TextMapCarrier
{
public:
  GrpcServerCarrier(ServerContext *context) : context_(context) {}
  GrpcServerCarrier() = default;
  virtual opentelemetry::nostd::string_view Get(
      opentelemetry::nostd::string_view key) const noexcept override
  {
    auto it = context_->client_metadata().find(key.data());
    if (it != context_->client_metadata().end())
    {
      return it->second.data();
    }
    return "";
  }

  virtual void Set(opentelemetry::nostd::string_view key,
                   opentelemetry::nostd::string_view value) noexcept override
  {
   // Not required for server
  }

  ServerContext *context_;
};

void initTracer()
{
  auto exporter = opentelemetry::exporter::otlp::OtlpGrpcExporterFactory::Create();
  auto processor =
      opentelemetry::sdk::trace::BatchSpanProcessorFactory::Create(std::move(exporter), {});
  std::vector<std::unique_ptr<opentelemetry::sdk::trace::SpanProcessor>> processors;
  processors.push_back(std::move(processor));

  auto context =
      opentelemetry::sdk::trace::TracerContextFactory::Create(std::move(processors));
  // Keep the concrete SDK provider so it can be flushed and shut down explicitly
  // during graceful termination.
  std::shared_ptr<opentelemetry::sdk::trace::TracerProvider> provider =
      opentelemetry::sdk::trace::TracerProviderFactory::Create(std::move(context));

  std::lock_guard<std::mutex> guard(provider_shutdown_mutex);
  tracer_provider = provider;

  // Set the global trace provider
  opentelemetry::trace::Provider::SetTracerProvider(
      opentelemetry::nostd::shared_ptr<opentelemetry::trace::TracerProvider>(provider));

  // set global propagator
  opentelemetry::context::propagation::GlobalTextMapPropagator::SetGlobalPropagator(
      opentelemetry::nostd::shared_ptr<opentelemetry::context::propagation::TextMapPropagator>(
          new opentelemetry::trace::propagation::HttpTraceContext()));
}

// Flush and shut down the tracer provider within the remaining shutdown
// budget. The concrete provider is kept alive (spans/handles may outlive the
// shutdown call); it is released explicitly in main after the API provider
// singleton has been cleared. Must be called from a normal thread (never from
// a signal handler).
void shutdownTracer(std::chrono::microseconds timeout)
{
  std::lock_guard<std::mutex> guard(provider_shutdown_mutex);
  if (!tracer_provider) return;

  if (tracer_provider->ForceFlush(timeout))
  {
    std::cout << "tracer provider flushed\n" << std::flush;
  }
  else
  {
    std::cerr << "tracer provider flush did not complete within the shutdown deadline\n";
  }

  if (tracer_provider->Shutdown())
  {
    std::cout << "tracer provider shut down\n" << std::flush;
  }
  else
  {
    std::cerr << "tracer provider shutdown failed\n";
  }
}

opentelemetry::nostd::shared_ptr<opentelemetry::trace::Tracer> get_tracer(std::string tracer_name)
{
  auto provider = opentelemetry::trace::Provider::GetTracerProvider();
  return provider->GetTracer(tracer_name);
}

} // namespace
