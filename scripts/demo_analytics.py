#!/usr/bin/env python
"""Demo data for the Superset dashboards, in its own `demo` namespace so it stays apart
from what smoke and chaos runs leave behind. Safe to run again: each step skips or no-ops
what is already there.

1. A synthetic dataset (`idNamespace=demo`, fixed seed), generated once.
2. Its transactions replayed through Kafka (a re-run is a no-op: dedup on eventId, T-5).
3. Its historical feedback (`feedback_events.jsonl`) loaded as impressions and
   interactions, model version `synthetic`, ids from the file so reloading adds nothing.
4. Live traffic through the customer API: recommendations, impressions of what was shown,
   clicks, promo activations and redemptions. Feedback must match a served slate (S-5), so
   it goes through the API like the app's. Skipped once demo customers have been served.

Local/test/ci only: it signs in with stand-in tokens and invents behaviour.

    python scripts/demo_analytics.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rec.settings import DEV_ENVIRONMENTS, settings  # noqa: E402

NS = "demo"
ADMIN = {"Authorization": "Bearer admin-token"}
DATASET = {"seed": 42, "idNamespace": NS, "customerCount": 400, "merchantCount": 120,
           "promotionCount": 25, "transactionCount": 8000, "historyDays": 120,
           "outputFormats": ["jsonl"]}


async def poll(client: httpx.AsyncClient, path: str, done, what: str, tries: int = 200):
    for _ in range(tries):
        r = await client.get(path, headers=ADMIN)
        r.raise_for_status()
        body = r.json()
        if done(body):
            return body
        if body.get("status") in ("FAILED", "STOPPED"):
            raise SystemExit(f"{what} {body['status']}: {body.get('error')}")
        await asyncio.sleep(3)
    raise SystemExit(f"{what} did not finish")


async def dataset(client: httpx.AsyncClient, db: asyncpg.Connection) -> str:
    found = await db.fetchval(
        """SELECT dataset_id FROM dataset_jobs WHERE config->>'idNamespace' = $1
           AND status = 'COMPLETED' ORDER BY created_at LIMIT 1""", NS)
    if found:
        return found
    r = await client.post("/admin/v1/datasets", headers=ADMIN, json=DATASET)
    r.raise_for_status()
    dataset_id = r.json()["datasetId"]
    await poll(client, f"/admin/v1/datasets/{dataset_id}",
               lambda d: d["status"] == "COMPLETED", "dataset")
    return dataset_id


async def replay(client: httpx.AsyncClient, db: asyncpg.Connection, dataset_id: str) -> int:
    r = await client.post("/admin/v1/simulations", headers=ADMIN,
                          json={"datasetId": dataset_id, "targetTps": 800})
    r.raise_for_status()
    run = r.json()["run_id"]
    (await client.post(f"/admin/v1/simulations/{run}/start", headers=ADMIN)).raise_for_status()
    await poll(client, f"/admin/v1/simulations/{run}", lambda s: s["status"] == "COMPLETED",
               "replay")
    count, previous = -1, -2
    while count != previous:  # the feature engine has drained once the count stops moving
        previous = count
        await asyncio.sleep(5)
        count = await db.fetchval(
            "SELECT count(*) FROM transaction_log WHERE customer_id LIKE $1", f"{NS}-%")
    return count


async def historical_feedback(db: asyncpg.Connection, dataset_id: str) -> dict[str, int]:
    path = Path(settings.data_dir) / dataset_id / "feedback_events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    for e in events:
        e["occurredAt"] = datetime.fromisoformat(e["occurredAt"])
    impressions = [(e["impressionId"], e["requestId"], e["customerId"], e["merchantId"],
                    e["position"], "synthetic", e["occurredAt"])
                   for e in events if e["eventType"] == "IMPRESSION"]
    interactions = [(f"{e['impressionId']}:{e['eventType']}", e["impressionId"],
                     e["customerId"], e["merchantId"], e["eventType"], e["occurredAt"])
                    for e in events if e["eventType"] != "IMPRESSION"]
    async with db.transaction():
        before = await db.fetchval("SELECT count(*) FROM impressions")
        await db.executemany(
            """INSERT INTO impressions (impression_id, request_id, customer_id, merchant_id,
                                        position, model_version, occurred_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT DO NOTHING""", impressions)
        added = await db.fetchval("SELECT count(*) FROM impressions") - before
        before = await db.fetchval("SELECT count(*) FROM interactions")
        await db.executemany(
            """INSERT INTO interactions (interaction_id, impression_id, customer_id,
                                         merchant_id, interaction_type, occurred_at)
               VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING""", interactions)
        return {"impressions": added,
                "interactions": await db.fetchval("SELECT count(*) FROM interactions") - before}


def click_probability(rank: int) -> float:
    """People click the top of a list more (the curve the position-debiasing model learns)."""
    return 0.35 / rank


async def one_customer(client: httpx.AsyncClient, cid: str, rng: random.Random,
                       totals: dict[str, int]) -> None:
    auth = {"Authorization": f"Bearer cust-{cid}"}
    for _ in range(rng.randint(1, 3)):  # later visits are usually served from cache
        r = await client.get(f"/api/v1/customer/{cid}/recommendations", headers=auth)
        if r.status_code != 200:
            totals["errors"] += 1
            return
        body = r.json()
        items = body["recommendations"][:rng.randint(4, len(body["recommendations"]) or 1)]
        if not items:
            continue
        shown = await client.post("/api/v1/feedback/impressions", headers=auth, json={
            "requestId": body["requestId"], "customerId": cid,
            "items": [{"merchantId": it["merchantId"], "position": it["rank"] - 1}
                      for it in items]})
        if shown.status_code != 202:
            totals["errors"] += 1
            continue
        totals["impressions"] += len(items)
        for impression_id, item in zip(shown.json()["impressionIds"], items, strict=True):
            steps = []
            if rng.random() < click_probability(item["rank"]):
                steps.append("CLICK")
                if item.get("promotion") and rng.random() < 0.4:
                    steps.append("PROMO_ACTIVATION")
                    if rng.random() < 0.5:
                        steps.append("REDEMPTION")
            for step in steps:
                done = await client.post("/api/v1/feedback/interactions", headers=auth, json={
                    "impressionId": impression_id, "customerId": cid,
                    "merchantId": item["merchantId"], "interactionType": step})
                totals[step if done.status_code == 202 else "errors"] += 1


async def live_traffic(client: httpx.AsyncClient, db: asyncpg.Connection,
                       seed: int) -> dict[str, int] | str:
    if await db.fetchval("SELECT 1 FROM recommendation_log WHERE customer_id LIKE $1 LIMIT 1",
                         f"{NS}-%"):
        return "skipped: demo customers were already served"
    ids = [r["customer_id"] for r in await db.fetch(
        "SELECT customer_id FROM customers WHERE customer_id LIKE $1 ORDER BY customer_id",
        f"{NS}-%")]
    rng = random.Random(seed)
    totals = {"customers": len(ids), "impressions": 0, "CLICK": 0, "PROMO_ACTIVATION": 0,
              "REDEMPTION": 0, "errors": 0}
    gate = asyncio.Semaphore(16)

    async def run(cid: str) -> None:
        async with gate:
            await one_customer(client, cid, random.Random(rng.random()), totals)

    await asyncio.gather(*(run(cid) for cid in ids))
    return totals


async def main(args) -> None:
    if settings.environment not in DEV_ENVIRONMENTS:
        raise SystemExit("demo data uses stand-in tokens and invented behaviour; "
                         "local/test/ci only")
    db = await asyncpg.connect(settings.postgres_dsn)
    try:
        async with httpx.AsyncClient(base_url=args.api, timeout=30) as client:
            dataset_id = await dataset(client, db)
            print("dataset", dataset_id)
            print("transactions for demo customers", await replay(client, db, dataset_id))
            print("historical feedback added", await historical_feedback(db, dataset_id))
            print("live traffic", await live_traffic(client, db, args.seed))
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--seed", type=int, default=7)
    asyncio.run(main(parser.parse_args()))
