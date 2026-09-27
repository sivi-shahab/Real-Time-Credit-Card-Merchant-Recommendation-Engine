"""Observability shared by api, ranking and stream: JSON logs with trace ids and
redaction (SEC-002), Prometheus metrics, and OpenTelemetry traces for api, ranking and
stream, carried across Kafka in message headers.

Redaction is a last line of defence; code should not log credentials in the first place.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.multiprocess import MultiProcessCollector

trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id",
                                                                          default=None)

_PATTERNS = [
    (re.compile(r"(?i)bearer\s+[\w\-.~+/=]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(rec_session|access_token|id_token|refresh_token|client_secret|"
                r"password|x-csrf-token)([\"']?\s*[=:]\s*[\"']?)[^\s&\"',;]+"),
     r"\1\2[REDACTED]"),
    (re.compile(r"(\w+://[^:/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),  # DSN passwords
    (re.compile(r"\b\d{13,19}\b"), "[PAN-REDACTED]"),  # card-number shaped digits
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        record.trace_id = trace_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage(),
               "traceId": getattr(record, "trace_id", None)}
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def metrics_body() -> tuple[bytes, str]:
    """The Prometheus scrape. With several uvicorn workers (WEB_CONCURRENCY) every worker
    writes to PROMETHEUS_MULTIPROC_DIR and whichever answers reports for all."""
    registry = REGISTRY
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        MultiProcessCollector(registry)
    return generate_latest(registry), CONTENT_TYPE_LATEST


def instrument(app=None) -> None:
    """Spans for inbound requests (FastAPI), calls out (httpx: api -> ranking, with W3C
    `traceparent`), Postgres (asyncpg: statements, never parameters) and Redis (command
    names only; keys and values are replaced with `?`, so no session ids)."""
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    for instrumentor in (HTTPXClientInstrumentor(), AsyncPGInstrumentor(),
                         RedisInstrumentor()):
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument()


def kafka_headers() -> list[tuple[str, bytes]]:
    """The current trace as Kafka headers (W3C `traceparent`); empty when not tracing."""
    from opentelemetry import propagate

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return [(key, value.encode()) for key, value in carrier.items()]


def kafka_context(headers):
    """The trace a consumed message carries, to parent the span that processes it."""
    from opentelemetry import propagate

    return propagate.extract({key: value.decode("latin-1") for key, value in headers or ()
                              if isinstance(value, bytes)})


def setup_tracing(service: str, app=None) -> bool:
    """OpenTelemetry, only when OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise nothing is
    installed and nothing costs anything. Everything else comes from the standard OTEL_*
    variables (OTEL_SERVICE_NAME, OTEL_TRACES_SAMPLER, ...)."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", service)}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    instrument(app)
    return True


# ------------------------------------------------------------------ metrics
# Route templates, not raw paths, as labels: raw paths would carry customer ids.
HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests",
                        ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP latency", ["route"],
                         buckets=(.01, .025, .05, .1, .2, .3, .5, 1, 2.5, 5))
RECOMMENDATIONS = Counter("recommendations_total", "Recommendations served",
                          ["source", "model_version"])
RANKING_DEGRADED = Counter("ranking_degraded_total", "Model failures served as baseline",
                           ["reason"])
EVENTS = Counter("stream_events_total", "Events handled by the feature engine", ["outcome"])
GUARDRAIL_ROLLBACKS = Counter("guardrail_rollbacks_total", "Automatic model rollbacks")
RATE_LIMITED = Counter("rate_limited_total", "Customer requests refused with 429 (D-1)",
                       ["bucket"])
INFERENCE_LATENCY = Histogram("ranking_inference_seconds", "Model predict time",
                              ["model_version"],
                              buckets=(.001, .0025, .005, .01, .025, .05, .1, .25, .5))

# Learning loops (ADR-0007, ADR-0010); alerts in deploy/prometheus/alerts.yml.
LEARNING_SWITCH = Gauge("learning_switch", "Learning settings in force on this replica",
                        ["name"],
                        multiprocess_mode="mostrecent")
AUTO_RETRAIN_RUNS = Counter("auto_retrain_runs_total", "Auto-retrain evaluations",
                            ["outcome"])  # queued | below_threshold | locked | failed
TRAINING_JOBS = Counter("training_jobs_total", "Training jobs finished",
                        ["trigger", "status"])
LIVE_EXPORTS_PRUNED = Counter("live_exports_pruned_total",
                              "Auto-retrain dataset exports deleted by retention")
AUTO_SHADOW = Counter("auto_shadow_promotions_total", "Automatic SHADOW promotions",
                      ["outcome"])  # promoted | skipped_serving | failed
BANDIT_PASSES = Counter("bandit_learning_passes_total", "Online bandit learning passes",
                        ["outcome"])  # ok | locked | failed
BANDIT_LEARNED = Counter("bandit_learned_impressions_total",
                         "Impressions the online bandit learned from")
BANDIT_LEARNED_UNTIL = Gauge("bandit_learned_until_timestamp_seconds",
                             "Impressions up to this time are learned (watermark)")
BANDIT_SHADOW = Counter("bandit_shadow_evaluations_total",
                        "Online bandit orderings recorded beside live requests", ["outcome"])
UPLIFT_REPORTS = Counter("uplift_reports_total", "Scheduled uplift report runs",
                         ["outcome"])  # saved | insufficient | locked | failed
PROMO_ASSIGNMENTS = Counter("promo_holdout_assignments_total",
                            "Customers newly assigned to a promo-holdout arm", ["arm"])
