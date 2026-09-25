"""SDD 15/16 performance checks. Writes a JSON result; numbers are only as good as the host.

  ingest  — drive the real FeatureProcessor over a dataset's replay file with the same
            per-customer concurrency the Kafka consumer uses (broker excluded: this
            measures the feature engine + Redis + Postgres, which is where the ceiling was).
  api     — concurrent GETs against a running recommendation API.

  python scripts/loadtest.py ingest --dataset test-dataset
  python scripts/loadtest.py api --base http://localhost:8000 --concurrency 32 --seconds 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2) if ordered else 0.0


async def ingest(args) -> dict:
    from rec.api import jobs
    from rec.settings import DEV_ENVIRONMENTS, settings
    from rec.store import pg
    from rec.store.redis_store import OnlineStore
    from rec.stream.processor import FeatureProcessor

    if settings.environment not in DEV_ENVIRONMENTS:
        raise SystemExit("ingest mode wipes Redis and transaction_log; dev environments only")
    out = Path(settings.data_dir) / args.dataset
    store = OnlineStore()
    await store.r.flushdb()
    conn = await pg.pool()
    await conn.execute("TRUNCATE transaction_log")
    await jobs.load_master_data(out)
    events = [json.loads(line) for line in (out / "replay.jsonl").read_text().splitlines()]
    events = events[: args.limit] if args.limit else events
    processor = FeatureProcessor(store, batch=True)
    gate = asyncio.Semaphore(settings.stream_concurrency)

    async def drain(batch: list[dict]) -> None:
        async with gate:
            for raw in batch:
                await processor.handle(raw)

    started = time.perf_counter()
    for i in range(0, len(events), 500):  # same shape as the consumer's getmany loop
        by_customer: dict[str, list[dict]] = {}
        for raw in events[i:i + 500]:
            by_customer.setdefault(str(raw.get("payload", {}).get("customerId")), []).append(raw)
        await asyncio.gather(*(drain(b) for b in by_customer.values()))
        await processor.flush()
    elapsed = time.perf_counter() - started
    await store.close()
    await pg.close()
    return {"events": len(events), "seconds": round(elapsed, 2),
            "eventsPerSecond": round(len(events) / elapsed, 1),
            "concurrency": settings.stream_concurrency}


async def api(args) -> dict:
    ids = [f"C{i:07d}" for i in range(1, args.customers + 1)]
    if args.customer_ids:
        ids = Path(args.customer_ids).read_text().split()
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    sources: dict[str, int] = {}
    deadline = time.perf_counter() + args.seconds

    async def worker(client: httpx.AsyncClient) -> None:
        while time.perf_counter() < deadline:
            cid = random.choice(ids)
            t0 = time.perf_counter()
            try:
                r = await client.get(f"/api/v1/customer/{cid}/recommendations",
                                     headers={"Authorization": f"Bearer cust-{cid}"})
                statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
                if r.status_code == 200:
                    src = r.json()["source"]
                    sources[src] = sources.get(src, 0) + 1
            except httpx.HTTPError:
                statuses[0] = statuses.get(0, 0) + 1
            latencies.append((time.perf_counter() - t0) * 1000)

    limits = httpx.Limits(max_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.base, timeout=5, limits=limits) as client:
        await asyncio.gather(*(worker(client) for _ in range(args.concurrency)))
    ok = sum(v for k, v in statuses.items() if k == 200)
    return {"requests": len(latencies), "rps": round(len(latencies) / args.seconds, 1),
            "concurrency": args.concurrency, "statuses": statuses, "sources": sources,
            "errorRate": round(1 - ok / len(latencies), 4) if latencies else None,
            "latencyMs": {"p50": pct(latencies, .50), "p95": pct(latencies, .95),
                          "p99": pct(latencies, .99),
                          "mean": round(statistics.fmean(latencies), 2) if latencies else 0}}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--dataset", default="test-dataset")
    p_ing.add_argument("--limit", type=int, default=0)
    p_api = sub.add_parser("api")
    p_api.add_argument("--base", default="http://localhost:8000")
    p_api.add_argument("--concurrency", type=int, default=32)
    p_api.add_argument("--seconds", type=int, default=30)
    p_api.add_argument("--customers", type=int, default=100)
    p_api.add_argument("--customer-ids", help="file with whitespace-separated ids")
    for p in (p_ing, p_api):
        p.add_argument("--out", help="append the JSON result to this file")
    args = parser.parse_args()
    result = asyncio.run(ingest(args) if args.mode == "ingest" else api(args))
    result |= {"mode": args.mode, "at": datetime.now(UTC).isoformat()}
    print(json.dumps(result, indent=2))
    if args.out:
        with open(args.out, "a") as fh:
            fh.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
