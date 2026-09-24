#!/usr/bin/env python
"""AC-008 — reconcile the online feature store against an offline recomputation.

Replays a dataset's events through the same pure ledger the stream uses and
compares the result with what Redis holds. Any drift is a bug in the online path.

    python scripts/reconcile.py <datasetId> [--as-of 2026-09-23T00:00:00Z] [--limit 200]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from rec.core.features import compute_features
from rec.core.ledger import CustomerState, Reject, apply_event
from rec.core.models import Envelope
from rec.settings import settings
from rec.store import pg
from rec.store.redis_store import OnlineStore

FIELDS = ("transactionCount90d", "netSpend90d", "averageSpend90d")


def offline_states(replay: Path, known: set[str], as_of: datetime) -> dict[str, CustomerState]:
    states: dict[str, CustomerState] = {}
    seen: set[str] = set()
    for line in replay.open():
        raw = json.loads(line)
        customer_id = raw.get("payload", {}).get("customerId")
        if customer_id not in known or raw["eventId"] in seen:
            continue
        try:
            env = Envelope.model_validate(raw)
        except Exception:
            continue
        seen.add(raw["eventId"])
        state = states.setdefault(customer_id, CustomerState(customer_id))
        try:
            apply_event(state, env, now=as_of)
        except (Reject, AssertionError, ValueError):
            continue
    return states


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_id")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    as_of = (datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of
             else datetime.now(UTC))
    replay = Path(settings.data_dir) / args.dataset_id / "replay.jsonl"
    if not replay.exists():
        print(f"replay file not found: {replay}", file=sys.stderr)
        return 2

    conn = await pg.pool()
    known = {r["customer_id"] for r in await conn.fetch("SELECT customer_id FROM customers")}
    states = offline_states(replay, known, as_of)

    store = OnlineStore()
    mismatches, checked = [], 0
    for customer_id, state in list(states.items())[: args.limit]:
        expected = compute_features(state, as_of)
        actual = await store.features(customer_id, as_of)
        diff = {f: (expected[f], actual[f]) for f in FIELDS if expected[f] != actual[f]}
        checked += 1
        if diff:
            mismatches.append({"customerId": customer_id, "diff": diff})
    await store.close()
    await pg.close()

    report = {
        "datasetId": args.dataset_id,
        "asOf": as_of.isoformat(),
        "customersChecked": checked,
        "mismatches": len(mismatches),
        "examples": mismatches[:10],
    }
    print(json.dumps(report, indent=2))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
