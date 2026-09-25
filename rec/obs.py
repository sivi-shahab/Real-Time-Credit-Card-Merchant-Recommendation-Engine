"""Observability shared by api, ranking and stream: JSON logs with trace ids and
redaction (SEC-002), plus Prometheus metrics.

Redaction is a last line of defence; code should not log credentials in the first place.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re

from prometheus_client import Counter, Histogram

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
INFERENCE_LATENCY = Histogram("ranking_inference_seconds", "Model predict time",
                              ["model_version"],
                              buckets=(.001, .0025, .005, .01, .025, .05, .1, .25, .5))
