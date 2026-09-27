"""SDD 15/16 performance checks. Writes a JSON result; numbers are only as good as the host.

  ingest  — drive the real FeatureProcessor over a dataset's replay file with the same
            per-customer concurrency the Kafka consumer uses (broker excluded: this
            measures the feature engine + Redis + Postgres, which is where the ceiling was).
  api     — concurrent GETs against a running recommendation API.
  kafka   — end to end through the broker (ADR-0014): publish copies of a dataset's replay
            with fresh ids to a benchmark topic and time until the consumer group has
            committed all of it. Start the consumers first, as many as you are measuring.

  python scripts/loadtest.py ingest --dataset test-dataset
  python scripts/loadtest.py kafka --topic bench.tx --group bench --copies 10
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


def replay_copies(lines: list[str], copies: int, run: str, distinct_customers: bool):
    """`copies` passes over a replay with fresh event and transaction ids, so every event
    is applied rather than de-duplicated; refunds and reversals point at their own copy's
    original. With `distinct_customers`, copy c goes to customers `<id>~c`."""
    for c in range(copies):
        suffix = f"-{run}-{c}"
        for line in lines:
            env = json.loads(line)
            env["eventId"] = str(env.get("eventId")) + suffix
            payload = env.get("payload") or {}
            for field in ("transactionId", "originalTransactionId"):
                if payload.get(field):
                    payload[field] += suffix
            if distinct_customers and payload.get("customerId"):
                payload["customerId"] += f"~{c}"  # the consumers' DB must hold these
            yield env


async def kafka(args) -> dict:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    from aiokafka.admin import AIOKafkaAdminClient

    from rec.settings import settings

    if args.topic == settings.topic_transactions:
        raise SystemExit("benchmark into its own topic, consumed by a group writing to a "
                         "benchmark database; never the live transactions topic")
    lines = (Path(settings.data_dir) / args.dataset / "replay.jsonl").read_text().splitlines()
    run = f"{random.getrandbits(32):08x}"

    def copies():
        return replay_copies(lines, args.copies, run, args.distinct_customers)

    producer = AIOKafkaProducer(bootstrap_servers=args.bootstrap, linger_ms=20,
                                max_batch_size=256 * 1024)
    await producer.start()
    started = time.perf_counter()
    sent = 0
    try:
        pending = []
        for env in copies():
            customer = str((env.get("payload") or {}).get("customerId", ""))
            pending.append(await producer.send(args.topic, json.dumps(env).encode(),
                                               key=customer.encode()))
            sent += 1
        await asyncio.gather(*pending)
    finally:
        await producer.stop()
    published = time.perf_counter() - started

    probe = AIOKafkaConsumer(bootstrap_servers=args.bootstrap)
    admin = AIOKafkaAdminClient(bootstrap_servers=args.bootstrap)
    await probe.start()
    await admin.start()
    try:
        ends = await probe.end_offsets(
            [p for p in await _partitions(probe, args.topic)])
        while True:
            committed = await admin.list_consumer_group_offsets(args.group)
            lag = sum(end - (committed[tp].offset if tp in committed else 0)
                      for tp, end in ends.items())
            if lag <= 0:
                break
            if time.perf_counter() - started > args.timeout:
                raise SystemExit(f"timed out with lag {lag}")
            await asyncio.sleep(0.2)
    finally:
        await probe.stop()
        await admin.close()
    elapsed = time.perf_counter() - started
    return {"events": sent, "partitions": len(ends), "publishSeconds": round(published, 2),
            "seconds": round(elapsed, 2), "eventsPerSecond": round(sent / elapsed, 1)}


async def _partitions(consumer, topic: str):
    from aiokafka.structs import TopicPartition

    for _ in range(50):
        found = consumer.partitions_for_topic(topic)
        if found:
            return [TopicPartition(topic, p) for p in sorted(found)]
        await consumer._client.force_metadata_update()  # noqa: SLF001
        await asyncio.sleep(0.1)
    raise SystemExit(f"topic {topic} not found")


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
    p_kafka = sub.add_parser("kafka")
    p_kafka.add_argument("--topic", required=True)
    p_kafka.add_argument("--group", required=True, help="the consumers' STREAM_GROUP_ID")
    p_kafka.add_argument("--dataset", default="test-dataset")
    p_kafka.add_argument("--copies", type=int, default=10)
    p_kafka.add_argument("--distinct-customers", action="store_true",
                         help="copy c goes to customers '<id>~c', as a real stream spreads "
                              "over many customers; create them in the consumers' DB first")
    p_kafka.add_argument("--bootstrap", default="localhost:29092")
    p_kafka.add_argument("--timeout", type=int, default=900)
    for p in (p_ing, p_api, p_kafka):
        p.add_argument("--out", help="append the JSON result to this file")
    args = parser.parse_args()
    result = asyncio.run({"ingest": ingest, "api": api, "kafka": kafka}[args.mode](args))
    result |= {"mode": args.mode, "at": datetime.now(UTC).isoformat()}
    print(json.dumps(result, indent=2))
    if args.out:
        with open(args.out, "a") as fh:
            fh.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
