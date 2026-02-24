"""OpenTelemetry bootstrap — traces, metrics, and logs for FastAPI.

Environment variables (standard OTEL spec):
    OTEL_EXPORTER_OTLP_ENDPOINT   — e.g. "http://localhost:4317"
    OTEL_SERVICE_NAME              — overrides the default service name
    OTEL_RESOURCE_ATTRIBUTES       — e.g. "deployment.environment=staging"

When OTEL_EXPORTER_OTLP_ENDPOINT is not set, telemetry is exported to
the console (stdout) as a development fallback.  When set, OTLP gRPC
is used exclusively.
"""

from __future__ import annotations

import logging
import os

import fastapi
from opentelemetry import metrics as metrics_api
from opentelemetry import trace as trace_api
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


def setup_otel(app: fastapi.FastAPI) -> None:
    """Configure OTEL providers and instrument the FastAPI app.

    OTLP endpoint and service name are read from standard OTEL
    environment variables.  When no OTLP endpoint is configured,
    console exporters are used as a development fallback.
    """
    resource = Resource.create({"service.name": "rtc-tools-bess-service"})
    has_otlp = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))

    # ── Traces ────────────────────────────────────────────────────
    tracer_provider = TracerProvider(resource=resource)
    if has_otlp:
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace_api.set_tracer_provider(tracer_provider)

    # ── Metrics ───────────────────────────────────────────────────
    if has_otlp:
        metric_readers = [
            PeriodicExportingMetricReader(OTLPMetricExporter()),
        ]
    else:
        metric_readers = [
            PeriodicExportingMetricReader(ConsoleMetricExporter()),
        ]
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=metric_readers,
    )
    metrics_api.set_meter_provider(meter_provider)

    # ── Logs ──────────────────────────────────────────────────────
    logger_provider = LoggerProvider(resource=resource)
    if has_otlp:
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter())
        )
    else:
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(ConsoleLogRecordExporter())
        )
    set_logger_provider(logger_provider)

    # Bridge stdlib logging → OTEL log records so existing _log.info()
    # calls in routes.py, solver_runner.py, etc. are captured.
    handler = LoggingHandler(
        level=logging.NOTSET,
        logger_provider=logger_provider,
    )
    logging.getLogger().addHandler(handler)

    # ── FastAPI instrumentation ───────────────────────────────────
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
