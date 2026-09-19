// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

#include "opentelemetry/exporters/otlp/otlp_grpc_exporter_factory.h"
#include "opentelemetry/logs/provider.h"
#include "opentelemetry/sdk/logs/logger.h"
#include "opentelemetry/sdk/logs/logger_provider_factory.h"
#include "opentelemetry/sdk/logs/batch_log_record_processor_factory.h"
#include "opentelemetry/sdk/logs/logger_context_factory.h"
#include "opentelemetry/exporters/otlp/otlp_grpc_log_record_exporter_factory.h"

#include <chrono>
#include <iostream>
#include <mutex>

using namespace std;
namespace nostd     = opentelemetry::nostd;
namespace otlp      = opentelemetry::exporter::otlp;
namespace logs      = opentelemetry::logs;
namespace logs_sdk  = opentelemetry::sdk::logs;

namespace
{
  // Concrete SDK logger provider, retained for explicit flush and shutdown.
  std::shared_ptr<logs_sdk::LoggerProvider> logger_provider;
  std::mutex logger_provider_shutdown_mutex;

  void initLogger() {
    otlp::OtlpGrpcLogRecordExporterOptions loggerOptions;
    auto exporter  = otlp::OtlpGrpcLogRecordExporterFactory::Create(loggerOptions);
    auto processor = logs_sdk::BatchLogRecordProcessorFactory::Create(std::move(exporter), {});
    std::vector<std::unique_ptr<logs_sdk::LogRecordProcessor>> processors;
    processors.push_back(std::move(processor));
    auto context = logs_sdk::LoggerContextFactory::Create(std::move(processors));
    std::shared_ptr<logs_sdk::LoggerProvider> provider = logs_sdk::LoggerProviderFactory::Create(std::move(context));

    {
      std::lock_guard<std::mutex> guard(logger_provider_shutdown_mutex);
      logger_provider = provider;
    }

    opentelemetry::logs::Provider::SetLoggerProvider(
        nostd::shared_ptr<logs::LoggerProvider>(provider));
  }

  // Flush and shut down the logger provider within the remaining shutdown
  // budget. The concrete provider is kept alive (the module-level Logger may
  // outlive the shutdown call); it is released explicitly in main after the
  // Logger handle has been dropped. Must be called from a normal thread (never
  // from a signal handler) and after the tracer/meter providers have been shut
  // down.
  void shutdownLogger(std::chrono::microseconds timeout) {
    std::lock_guard<std::mutex> guard(logger_provider_shutdown_mutex);
    if (!logger_provider) return;

    if (logger_provider->ForceFlush(timeout))
    {
      std::cout << "logger provider flushed\n" << std::flush;
    }
    else
    {
      std::cerr << "logger provider flush did not complete within the shutdown deadline\n";
    }

    if (logger_provider->Shutdown())
    {
      std::cout << "logger provider shut down\n" << std::flush;
    }
    else
    {
      std::cerr << "logger provider shutdown failed\n";
    }
  }

  nostd::shared_ptr<opentelemetry::logs::Logger> getLogger(std::string name){
    auto provider = logs::Provider::GetLoggerProvider();
    return provider->GetLogger(name + "_logger", name, OPENTELEMETRY_SDK_VERSION);
  }
}
