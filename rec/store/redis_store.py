"""Online store (SDD 3.3): rebuildable, never the sole history.

One Redis hash per customer holds the whole CustomerState so the pure ledger in
rec.core is the single implementation for both online and offline paths.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import redis.asyncio as redis

from rec.core.features import compute_features
from rec.core.ledger import CustomerState, TxnRecord, prune
from rec.settings import settings

STATE_PREFIX = "state:"
DEDUP_PREFIX = "evt:"
CACHE_PREFIX = "rec:"
CACHE_INDEX_PREFIX = "recidx:"
ERASED_KEY = "erased"  # copy of the erased_customers tombstones (AC-009)
SERVED_PREFIX = "served:"  # + customerId + ":" + requestId -> merchant -> position (S-5)
SERVED_TTL_SECONDS = 24 * 3600  # feedback for a slate must arrive within a day
MODEL_FIELD = "__model"


def _client() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=True,
                          max_connections=settings.redis_max_connections)


class OnlineStore:
    def __init__(self, client: redis.Redis | None = None):
        self.r = client or _client()

    # ---------------------------------------------------------- dedup
    async def claim_event(self, event_id: str) -> bool:
        """True if this eventId has not been seen (EVT-003 idempotency)."""
        return bool(await self.r.set(DEDUP_PREFIX + event_id, "1", nx=True,
                                     ex=settings.dedup_ttl_seconds))

    async def release_event(self, event_id: str) -> None:
        await self.r.delete(DEDUP_PREFIX + event_id)

    # ---------------------------------------------------------- state
    async def load(self, customer_id: str) -> CustomerState:
        raw = await self.r.hgetall(STATE_PREFIX + customer_id)
        state = CustomerState(customerId=customer_id)
        for field, value in raw.items():
            if field.startswith("b:"):
                day, cat, merch = field[2:].split("|", 2)
                count, net = value.split(",")
                state.buckets[(day, cat, merch)] = [int(count), int(net)]
            elif field.startswith("t:"):
                day, cat, merch, amount, refunded, rev = value.split("|", 5)
                state.txns[field[2:]] = TxnRecord(
                    day, cat, merch, int(amount), int(refunded), rev == "1"
                )
            elif field.startswith("h:"):
                state.hours[int(field[2:])] = int(value)
            elif field == "lastEventOccurredAt" and value:
                state.lastEventOccurredAt = datetime.fromisoformat(value)
            elif field == "featureVersion":
                state.featureVersion = int(value)
        return state

    async def save(self, state: CustomerState, as_of: datetime) -> None:
        """Persist only the fields this event touched, plus anything pruned."""
        prune(state, as_of)
        key = STATE_PREFIX + state.customerId
        mapping = {
            "featureVersion": str(state.featureVersion),
            "lastEventOccurredAt": state.lastEventOccurredAt.isoformat()
            if state.lastEventOccurredAt
            else "",
        }
        for bucket_key in state.dirtyBuckets:
            bucket = state.buckets.get(bucket_key)
            if bucket is not None:
                day, cat, merch = bucket_key
                mapping[f"b:{day}|{cat}|{merch}"] = f"{bucket[0]},{bucket[1]}"
        for txn_id in state.dirtyTxns:
            rec = state.txns.get(txn_id)
            if rec is not None:
                mapping[f"t:{txn_id}"] = (
                    f"{rec.day}|{rec.categoryCode}|{rec.merchantId}|"
                    f"{rec.amountMinor}|{rec.refundedMinor}|{int(rec.reversed)}"
                )
        for hour in state.dirtyHours:
            mapping[f"h:{hour}"] = str(state.hours[hour])
        removed = [f"b:{d}|{c}|{m}" for d, c, m in state.removedBuckets]
        removed += [f"t:{t}" for t in state.removedTxns]

        async with self.r.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=mapping)
            if removed:
                pipe.hdel(key, *removed)
            await pipe.execute()
        state.clear_dirty()

    async def features(self, customer_id: str, as_of: datetime | None = None,
                       merchant_city: dict[str, str] | None = None) -> dict:
        as_of = as_of or datetime.now(UTC)
        state = await self.load(customer_id)
        return compute_features(state, as_of, merchant_city=merchant_city)

    async def known_customers(self, limit: int = 200) -> list[str]:
        out = []
        async for key in self.r.scan_iter(match=STATE_PREFIX + "*", count=200):
            out.append(key[len(STATE_PREFIX):])
            if len(out) >= limit:
                break
        return sorted(out)

    # ---------------------------------------------------------- cache
    async def get_cached(self, key: str) -> dict | None:
        raw = await self.r.get(CACHE_PREFIX + key)
        return json.loads(raw) if raw else None

    async def put_cached(self, key: str, value: dict, ttl: int | None = None) -> None:
        ttl = ttl or settings.recommendation_cache_ttl_seconds
        customer_id = key.split("|", 1)[0]
        index = CACHE_INDEX_PREFIX + customer_id
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(CACHE_PREFIX + key, json.dumps(value, default=str), ex=ttl)
            pipe.sadd(index, key)
            pipe.expire(index, ttl * 4)
            await pipe.execute()

    async def invalidate_customer(self, customer_id: str) -> int:
        """Indexed invalidation — a keyspace SCAN per ingested event does not scale."""
        index = CACHE_INDEX_PREFIX + customer_id
        keys = await self.r.smembers(index)
        if not keys:
            return 0
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.delete(*[CACHE_PREFIX + k for k in keys])
            pipe.delete(index)
            result = await pipe.execute()
        return int(result[0])

    async def invalidate_all_recommendations(self) -> int:
        """Catalog-wide change (merchant/promo/model promotion) — SERV-004."""
        deleted = 0
        for pattern in (CACHE_PREFIX + "*", CACHE_INDEX_PREFIX + "*"):
            async for key in self.r.scan_iter(match=pattern, count=500):
                deleted += await self.r.delete(key)
        return deleted

    # ---------------------------------------------------------- erasure (AC-009)
    async def is_erased(self, customer_id: str) -> bool:
        return bool(await self.r.sismember(ERASED_KEY, customer_id))

    async def load_tombstones(self, customer_ids) -> None:
        """Re-hydrate after Redis loss; Postgres holds the durable list."""
        if customer_ids:
            await self.r.sadd(ERASED_KEY, *customer_ids)

    async def erase_customer(self, customer_id: str) -> int:
        await self.r.sadd(ERASED_KEY, customer_id)  # tombstone first: stops new writes
        deleted = await self.r.delete(STATE_PREFIX + customer_id)
        served = [k async for k in self.r.scan_iter(match=f"{SERVED_PREFIX}{customer_id}:*")]
        if served:
            deleted += await self.r.delete(*served)
        return deleted + await self.invalidate_customer(customer_id)

    # ---------------------------------------------------------- served slates (S-5)
    async def record_served(self, customer_id: str, request_id: str, model_version: str,
                            merchant_ids: list[str]) -> None:
        """What this customer was shown, so feedback can be checked against it."""
        key = f"{SERVED_PREFIX}{customer_id}:{request_id}"
        mapping = {m: position for position, m in enumerate(merchant_ids)}
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={**mapping, MODEL_FIELD: model_version})
            pipe.expire(key, SERVED_TTL_SECONDS)
            await pipe.execute()

    async def served(self, customer_id: str, request_id: str) -> dict[str, str]:
        return await self.r.hgetall(f"{SERVED_PREFIX}{customer_id}:{request_id}")

    # ---------------------------------------------------------- ops
    async def incr_metric(self, name: str, amount: int = 1) -> None:
        await self.r.hincrby("metrics", name, amount)

    async def incr_metrics(self, counts: dict[str, int]) -> None:
        if not counts:
            return
        async with self.r.pipeline(transaction=False) as pipe:
            for name, amount in counts.items():
                pipe.hincrby("metrics", name, amount)
            await pipe.execute()

    async def metrics(self) -> dict[str, int]:
        return {k: int(v) for k, v in (await self.r.hgetall("metrics")).items()}

    async def close(self) -> None:
        await self.r.aclose()
