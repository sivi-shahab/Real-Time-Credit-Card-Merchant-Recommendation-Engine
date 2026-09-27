"""Distributed tracing: one trace from the API into the ranking service, the API's traceId
is that trace's id, and spans carry no parameter values."""
import json
import os
import subprocess
import sys

SCRIPT = r"""
import asyncio, json, socket, threading, time
import httpx, uvicorn
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rec.api.app import app as api_app          # setup_tracing runs at import
from rec.ranking.service import app as ranking_app
from rec.ml import client as ranking_client
from rec.settings import settings

memory = InMemorySpanExporter()
trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(memory))

with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(ranking_app, port=port, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started: time.sleep(0.05)
settings.ranking_service_url = f"http://127.0.0.1:{port}"

async def main():
    out = {}
    with trace.get_tracer("test").start_as_current_span("promote") as span:
        out["root"] = format(span.get_span_context().trace_id, "032x")
        try:
            await ranking_client.warm("absent", "0" * 64)
        except ranking_client.RankingUnavailable as exc:
            out["warm"] = exc.reason
    await ranking_client.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app),
                                 base_url="http://t") as c:
        from rec.api.app import lifespan
        async with lifespan(api_app):
            r = await c.get("/api/v1/customer/C0000001/recommendations",
                            headers={"Authorization": "Bearer cust-C0000001"})
            out["status"], out["correlation"] = r.status_code, r.headers["x-correlation-id"]
    spans = [{"name": x.name, "trace": format(x.context.trace_id, "032x"),
              "service": x.resource.attributes.get("service.name"),
              "attrs": {k: str(v) for k, v in (x.attributes or {}).items()}}
             for x in memory.get_finished_spans()]
    out["spans"] = spans
    print("RESULT" + json.dumps(out))

asyncio.run(main())
server.should_exit = True
"""


def test_one_trace_spans_api_and_ranking_and_carries_no_values():
    env = os.environ | {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
                        "OTEL_BSP_SCHEDULE_DELAY": "60000"}
    done = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True,
                          text=True, timeout=120)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), None)
    assert line, done.stderr[-2000:]
    out = json.loads(line[len("RESULT"):])

    # api -> ranking: the ranking service's server span joins the caller's trace
    assert out["warm"] == "RANKING_WARM_HTTP_404"
    # (both apps share one process and so one tracer provider here; in the stack each
    # service exports under its own name)
    in_root = {s["name"] for s in out["spans"] if s["trace"] == out["root"]}
    assert "POST /v1/models/{model_version}/warm" in in_root, in_root   # ranking's server
    assert "POST" in in_root, in_root                                   # the API's client

    # the API's own request: its traceId is the OpenTelemetry trace id
    assert out["status"] == 200
    request_spans = [s for s in out["spans"] if s["trace"] == out["correlation"]]
    names = " ".join(s["name"] for s in request_spans)
    assert "GET /api/v1/customer/{customer_id}/recommendations" in names, names
    assert any(s["attrs"].get("db.system") == "postgresql" for s in request_spans)
    assert any(s["attrs"].get("db.system") == "redis" for s in request_spans)

    # statements and commands, never the values bound into them
    for s in out["spans"]:
        if s["attrs"].get("db.system") in ("postgresql", "redis"):
            flat = json.dumps(s["attrs"])
            assert "C0000001" not in flat and "sess:" not in flat, s
