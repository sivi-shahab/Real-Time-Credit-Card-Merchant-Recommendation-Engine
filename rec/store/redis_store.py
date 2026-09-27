"""Online store (SDD 3.3): rebuildable, never the sole history.

Per customer (ADR-0014), every key tagged `{customerId}` so a customer's keys share one
Redis Cluster slot and its writes stay one transaction:
- `state:{C}` hash: the day buckets and hour counts features are computed from, read
  whole on every event (bounded by the 90-day window);
- `txn:{C}:<YYYY-MM>` hashes: the transaction ledger kept 180 days to match refunds and
  reversals, by month of the transaction day. An event reads only the one or two records
  it names; each month's hash expires on its own once all of it is past retention;
- `erased:{C}`: the erasure tombstone (AC-009);
- `recidx:{C}`, `rec:{C}|...`, `served:{C}:<requestId>`: cache and served slates.
The pure ledger in rec.core stays the single implementation for online and offline paths.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta

import redis.asyncio as redis
from redis.asyncio.cluster import RedisCluster

from rec.core.features import compute_features
from rec.core.ledger import TXN_RETENTION_DAYS, CustomerState, TxnRecord, prune
from rec.settings import settings

DEDUP_PREFIX = "evt:"
CACHE_PREFIX = "rec:"
CACHE_INDEX_PREFIX = "recidx:"
SERVED_TTL_SECONDS = 24 * 3600  # feedback for a slate must arrive within a day
MODEL_FIELD = "__model"


def state_key(customer_id: str) -> str:
    return f"state:{{{customer_id}}}"


def txn_key(customer_id: str, month: str) -> str:
    return f"txn:{{{customer_id}}}:{month}"


def erased_key(customer_id: str) -> str:
    return f"erased:{{{customer_id}}}"


def cache_index_key(customer_id: str) -> str:
    return f"{CACHE_INDEX_PREFIX}{{{customer_id}}}"


def cache_redis_key(key: str) -> str:
    """Cache keys start with the customer id (service.cache_key); tag it."""
    customer_id, _, rest = key.partition("|")
    return f"{CACHE_PREFIX}{{{customer_id}}}|{rest}"


def served_key(customer_id: str, request_id: str) -> str:
    return f"served:{{{customer_id}}}:{request_id}"


def _client():
    if settings.redis_cluster:
        client = RedisCluster.from_url(settings.redis_url, decode_responses=True,
                                       max_connections=settings.redis_max_connections)
        # redis-py 8.1: a cluster MULTI hands its routing `keys` to the response
        # callbacks, most of which take no such argument, so the transaction runs and
        # then raises TypeError (SMEMBERS, EXPIRE, EXPIREAT here). Drop that argument.
        for name, callback in list(client.response_callbacks.items()):
            if callable(callback):
                client.set_response_callback(name, _without_keys(callback))
        return client
    return redis.from_url(settings.redis_url, decode_responses=True,
                          max_connections=settings.redis_max_connections)


def _without_keys(callback):
    def call(response, **options):
        options.pop("keys", None)
        return callback(response, **options)
    return call


def _parse_state(customer_id: str, raw: dict[str, str]) -> CustomerState:
    state = CustomerState(customerId=customer_id)
    for field, value in raw.items():
        if field.startswith("b:"):
            day, cat, merch = field[2:].split("|", 2)
            count, net = value.split(",")
            state.buckets[(day, cat, merch)] = [int(count), int(net)]
        elif field.startswith("h:"):
            state.hours[int(field[2:])] = int(value)
        elif field == "lastEventOccurredAt" and value:
            state.lastEventOccurredAt = datetime.fromisoformat(value)
        elif field == "featureVersion":
            state.featureVersion = int(value)
    return state


def _txn_value(rec: TxnRecord) -> str:
    return (f"{rec.day}|{rec.categoryCode}|{rec.merchantId}|"
            f"{rec.amountMinor}|{rec.refundedMinor}|{int(rec.reversed)}")


def _parse_txn(value: str) -> TxnRecord:
    day, cat, merch, amount, refunded, rev = value.split("|", 5)
    return TxnRecord(day, cat, merch, int(amount), int(refunded), rev == "1")


def ledger_months(as_of: datetime) -> list[str]:
    """Months that can hold a record still inside retention at `as_of` (at most 7)."""
    start = (as_of.astimezone(UTC) - timedelta(days=TXN_RETENTION_DAYS)).date()
    end = as_of.astimezone(UTC).date()
    months, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def month_expiry(month: str) -> int:
    """When the last record of `month` leaves retention: the key can go then."""
    year, mon = map(int, month.split("-"))
    first_of_next = date(year + (mon == 12), mon % 12 + 1, 1)
    until = first_of_next + timedelta(days=TXN_RETENTION_DAYS + 1)
    return int(datetime.combine(until, time.min, UTC).timestamp())


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
        """Buckets and hours: all features need. The ledger is read per event."""
        return _parse_state(customer_id, await self.r.hgetall(state_key(customer_id)))

    async def admit(self, customer_id: str | None, event_id: str | None,
                    txn_ids: list[str] | tuple = (), *, as_of: datetime | None = None,
                    ) -> tuple[bool, bool, CustomerState | None]:
        """One round trip for the ingest hot path: (erased, claimed, state).

        The claim and state read happen only when `event_id` is given, i.e. for a valid
        event of a known customer. The state carries only the ledger records in
        `txn_ids` (the event's own transaction, and the original it corrects): the
        ledger needs no others, and reading all of it was half of Redis's work
        (ADR-0014). A claim made for an erased customer is left to expire: erasure is
        permanent, so that event can never need applying.
        """
        as_of = as_of or datetime.now(UTC)
        months = ledger_months(as_of) if event_id is not None and txn_ids else []
        async with self.r.pipeline(transaction=False) as pipe:
            pipe.exists(erased_key(customer_id or ""))
            if event_id is not None:
                pipe.set(DEDUP_PREFIX + event_id, "1", nx=True, ex=settings.dedup_ttl_seconds)
                pipe.hgetall(state_key(customer_id))
                for month in months:
                    pipe.hmget(txn_key(customer_id, month), *txn_ids)
            result = await pipe.execute()
        if event_id is None:
            return bool(result[0]), False, None
        state = _parse_state(customer_id, result[2])
        cutoff = (as_of.astimezone(UTC).date() - timedelta(days=TXN_RETENTION_DAYS)).isoformat()
        for values in result[3:]:
            for txn_id, value in zip(txn_ids, values, strict=True):
                if value:
                    record = _parse_txn(value)
                    if record.day >= cutoff:  # same rule as prune: aged out is absent
                        state.txns[txn_id] = record
        return bool(result[0]), bool(result[1]), state

    async def save(self, state: CustomerState, as_of: datetime) -> None:
        """Persist only what this event touched, and drop the customer's cached
        recommendations (SERV-004): one transaction, in the customer's slot, plus a
        delete if some are cached."""
        prune(state, as_of)
        customer_id = state.customerId
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
        for hour in state.dirtyHours:
            mapping[f"h:{hour}"] = str(state.hours[hour])
        removed = [f"b:{d}|{c}|{m}" for d, c, m in state.removedBuckets]
        # Ledger records go to the month of their day. Pruned ones need no delete: they
        # are ignored on read and their month's key expires.
        ledger: dict[str, dict[str, str]] = {}
        for txn_id in state.dirtyTxns:
            record = state.txns.get(txn_id)
            if record is not None:
                ledger.setdefault(record.day[:7], {})[txn_id] = _txn_value(record)

        index = cache_index_key(customer_id)
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.hset(state_key(customer_id), mapping=mapping)
            if removed:
                pipe.hdel(state_key(customer_id), *removed)
            for month, records in ledger.items():
                pipe.hset(txn_key(customer_id, month), mapping=records)
                pipe.expireat(txn_key(customer_id, month), month_expiry(month))
            pipe.smembers(index)
            cached = (await pipe.execute())[-1]
        state.clear_dirty()
        if cached:
            await self.r.delete(*[cache_redis_key(k) for k in cached], index)

    async def features(self, customer_id: str, as_of: datetime | None = None,
                       merchant_city: dict[str, str] | None = None) -> dict:
        as_of = as_of or datetime.now(UTC)
        state = await self.load(customer_id)
        return compute_features(state, as_of, merchant_city=merchant_city)

    async def known_customers(self, limit: int = 200) -> list[str]:
        out = []
        async for key in self.r.scan_iter(match="state:{*}", count=200):
            out.append(key[len("state:{"):-1])
            if len(out) >= limit:
                break
        return sorted(out)

    # ---------------------------------------------------------- cache
    async def get_cached(self, key: str) -> dict | None:
        raw = await self.r.get(cache_redis_key(key))
        return json.loads(raw) if raw else None

    async def put_cached(self, key: str, value: dict, ttl: int | None = None) -> None:
        ttl = ttl or settings.recommendation_cache_ttl_seconds
        customer_id = key.split("|", 1)[0]
        index = cache_index_key(customer_id)
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(cache_redis_key(key), json.dumps(value, default=str), ex=ttl)
            pipe.sadd(index, key)
            pipe.expire(index, ttl * 4)
            await pipe.execute()

    async def invalidate_customer(self, customer_id: str) -> int:
        """Indexed invalidation — a keyspace SCAN per ingested event does not scale."""
        index = cache_index_key(customer_id)
        keys = await self.r.smembers(index)
        if not keys:
            return 0
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.delete(*[cache_redis_key(k) for k in keys])
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
        return bool(await self.r.exists(erased_key(customer_id)))

    async def load_tombstones(self, customer_ids) -> None:
        """Re-hydrate after Redis loss; Postgres holds the durable list. One key per
        customer, not one set: every event checks it, and a set is one hot slot."""
        ids = list(customer_ids or ())
        for start in range(0, len(ids), 1000):
            async with self.r.pipeline(transaction=False) as pipe:
                for customer_id in ids[start:start + 1000]:
                    pipe.set(erased_key(customer_id), "1")
                await pipe.execute()

    async def erase_customer(self, customer_id: str) -> int:
        await self.r.set(erased_key(customer_id), "1")  # tombstone first: stops new writes
        keys = [state_key(customer_id)]
        for pattern in (f"txn:{{{customer_id}}}:*", f"served:{{{customer_id}}}:*"):
            keys += [k async for k in self.r.scan_iter(match=_glob(pattern))]
        deleted = 0
        for key in keys:  # one by one: they may live on different cluster nodes
            deleted += await self.r.delete(key)
        return deleted + await self.invalidate_customer(customer_id)

    # ---------------------------------------------------------- served slates (S-5)
    async def record_served(self, customer_id: str, request_id: str, model_version: str,
                            merchant_ids: list[str]) -> None:
        """What this customer was shown, so feedback can be checked against it."""
        key = served_key(customer_id, request_id)
        mapping = {m: position for position, m in enumerate(merchant_ids)}
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={**mapping, MODEL_FIELD: model_version})
            pipe.expire(key, SERVED_TTL_SECONDS)
            await pipe.execute()

    async def served(self, customer_id: str, request_id: str) -> dict[str, str]:
        return await self.r.hgetall(served_key(customer_id, request_id))

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


def _glob(pattern: str) -> str:
    """Escape the customer id's glob characters in a SCAN pattern (ids are not trusted
    to be free of `*?[`); the trailing `*` stays a wildcard."""
    head, tail = pattern[:-1], pattern[-1]
    return "".join("\\" + ch if ch in "*?[]\\" else ch for ch in head) + tail
