// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

#include "opentelemetry/exporters/otlp/otlp_grpc_metric_exporter_factory.h"
#include "opentelemetry/metrics/provider.h"
#include "opentelemetry/sdk/metrics/export/periodic_exporting_metric_reader.h"
#include "opentelemetry/sdk/metrics/meter.h"
#include "opentelemetry/sdk/metrics/meter_provider.h"

#include <chrono>
#include <iostream>
#include <mutex>

// namespaces
namespace common        = opentelemetry::common;
namespace metrics_api   = opentelemetry::metrics;
namespace metric_sdk    = opentelemetry::sdk::metrics;
namespace nostd         = opentelemetry::nostd;
namespace otlp_exporter = opentelemetry::exporter::otlp;

namespace
{
  // Concrete SDK meter provider, retained for explicit flush and shutdown.
  nostd::shared_ptr<metric_sdk::MeterProvider> meter_provider;
  std::mutex meter_provider_shutdown_mutex;

  void initMeter()
  {
    // Build MetricExporter
    otlp_exporter::OtlpGrpcMetricExporterOptions otlpOptions;
    auto exporter = otlp_exporter::OtlpGrpcMetricExporterFactory::Create(otlpOptions);

    // Build MeterProvider and Reader
    metric_sdk::PeriodicExportingMetricReaderOptions options;
    std::unique_ptr<metric_sdk::MetricReader> reader{
        new metric_sdk::PeriodicExportingMetricReader(std::move(exporter), options) };
    // Keep concrete (SDK) ownership in a module-level handle so the provider can
    // be flushed and shut down explicitly during graceful termination.
    nostd::shared_ptr<metric_sdk::MeterProvider> concrete_provider(new metric_sdk::MeterProvider());
    concrete_provider->AddMetricReader(std::move(reader));

    {
      std::lock_guard<std::mutex> guard(meter_provider_shutdown_mutex);
      meter_provider = concrete_provider;
    }

    metrics_api::Provider::SetMeterProvider(
        nostd::shared_ptr<metrics_api::MeterProvider>(
            static_cast<metrics_api::MeterProvider*>(concrete_provider.get())));
  }

  // Flush and shut down the meter provider within the remaining shutdown
  // budget. The concrete provider handle is intentionally NOT released here
  // (main leaks it after shutdown): in SDK 1.27 destroying the provider after
  // Shutdown() re-enters MeterContext::Shutdown from the reader destructor and
  // crashes the OTLP gRPC metric exporter teardown.
  // Must be called from a normal thread (never from a signal handler).
  void shutdownMeter(std::chrono::microseconds timeout)
  {
    std::lock_guard<std::mutex> guard(meter_provider_shutdown_mutex);
    if (!meter_provider) return;

    if (meter_provider->ForceFlush(timeout))
    {
      std::cout << "meter provider flushed\n" << std::flush;
    }
    else
    {
      std::cerr << "meter provider flush did not complete within the shutdown deadline\n";
    }

    if (meter_provider->Shutdown())
    {
      std::cout << "meter provider shut down\n" << std::flush;
    }
    else
    {
      std::cerr << "meter provider shutdown failed\n";
    }
  }

  nostd::unique_ptr<metrics_api::Counter<uint64_t>> initIntCounter(std::string name, std::string version)
  {
    std::string counter_name = name + "_counter";
    auto provider = metrics_api::Provider::GetMeterProvider();
    nostd::shared_ptr<metrics_api::Meter> meter = provider->GetMeter(name, version);
    auto int_counter = meter->CreateUInt64Counter(counter_name);
    return int_counter;
  }
}
