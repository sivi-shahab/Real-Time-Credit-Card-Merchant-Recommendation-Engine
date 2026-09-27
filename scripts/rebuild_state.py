#!/usr/bin/env python
"""Rebuild the Redis online store from Postgres (SDD 3.3: Redis is rebuildable, never the
sole history). Use after Redis loss or a restore — see docs/runbooks.md.

Re-runs every logged envelope, in original processing order, through the same
FeatureProcessor; tombstones are loaded first so erased customers stay erased (AC-009).

    python scripts/rebuild_state.py [--flush] [--now 2026-09-23T01:00:00+00:00]

--now pins the evaluation clock (tests/drills on historical data); omit it in production.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime

from rec.settings import settings
from rec.store import pg
from rec.store.redis_store import OnlineStore
from rec.stream.processor import FeatureProcessor


async def main(flush: bool, now: datetime | None) -> None:
    store = OnlineStore()
    if flush:
        await store.r.flushdb()
    await store.load_tombstones(await pg.erased_customers())
    processor = FeatureProcessor(store, batch=True)
    gate = asyncio.Semaphore(settings.stream_concurrency)
    conn = await pg.pool()
    outcomes: dict[str, int] = {}
    started, last = time.perf_counter(), 0
    while True:  # keyset pages: the log can be far larger than memory
        rows = await conn.fetch("""SELECT seq, envelope FROM transaction_log WHERE seq > $1
                                   ORDER BY seq LIMIT 5000""", last)
        if not rows:
            break
        # Order only matters per customer, exactly as in the live consumer.
        by_customer: dict[str, list[dict]] = {}
        for row in rows:
            raw = json.loads(row["envelope"])
            if not isinstance(raw, dict):
                continue
            # quarantined rows can hold any payload at all (D-4)
            payload = raw.get("payload")
            customer = payload.get("customerId") if isinstance(payload, dict) else None
            by_customer.setdefault(str(customer), []).append(raw)

        async def drain(events: list[dict]) -> None:
            async with gate:
                for raw in events:
                    code = await processor.handle(raw, now=now)
                    outcomes[code] = outcomes.get(code, 0) + 1

        await asyncio.gather(*(drain(e) for e in by_customer.values()))
        await processor.flush()
        last = rows[-1]["seq"]
    await store.invalidate_all_recommendations()
    print(json.dumps({"outcomes": outcomes,
                      "seconds": round(time.perf_counter() - started, 1)}, indent=2))
    await store.close()
    await pg.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--flush", action="store_true", help="wipe the Redis DB first")
    parser.add_argument("--now", type=datetime.fromisoformat, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.flush, args.now))
