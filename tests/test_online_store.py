"""ADR-0014: the ledger is read per event by id, and a customer's keys share one slot."""
from datetime import UTC, datetime, timedelta

import pytest_asyncio
import redis.asyncio as redis
from redis.crc import key_slot

from rec.core.ledger import CustomerState, TxnRecord, apply_event
from rec.core.models import Envelope
from rec.settings import settings
from rec.store import redis_store
from rec.store.redis_store import OnlineStore, state_key

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def store():
    if settings.redis_cluster:  # a cluster has one database; these keys are its own
        s = OnlineStore()
        keys = [k async for k in s.r.scan_iter(match="*{C[12]}*")]
        keys += [k async for k in s.r.scan_iter(match="evt:e-*")]
        for key in keys:
            await s.r.delete(key)
        yield s
        await s.close()
        return
    # its own database: the E2E suite keeps its state in the configured one
    base = settings.redis_url.rsplit("/", 1)[0]
    s = OnlineStore(redis.from_url(f"{base}/5", decode_responses=True))
    await s.r.flushdb()
    yield s
    await s.r.flushdb()
    await s.close()


def _event(n: str, kind: str, when: datetime, amount: int, original: str | None = None):
    return Envelope.model_validate({
        "eventId": f"e-{n}", "eventType": "transaction.created", "eventVersion": "1.0.0",
        "occurredAt": when.isoformat(), "producer": "test", "correlationId": n,
        "payload": {"transactionId": f"t-{n}", "customerId": "C1", "merchantId": "M1",
                    "categoryCode": "FOOD", "cityCode": "JKT", "amountMinor": amount,
                    "currency": "IDR", "transactionType": kind, "occurredAt": when.isoformat(),
                    "originalTransactionId": original}})


async def _apply(store, env: Envelope) -> str:
    ids = [t for t in (env.payload["transactionId"], env.payload.get("originalTransactionId"))
           if t]
    _, claimed, state = await store.admit("C1", env.eventId, ids, as_of=NOW)
    assert claimed
    outcome = apply_event(state, env, now=NOW)
    await store.save(state, NOW)
    return outcome


async def test_the_ledger_is_read_by_id_across_months(store):
    march, may = NOW - timedelta(days=170), NOW - timedelta(days=120)
    assert await _apply(store, _event("1", "PURCHASE", march, 1000)) == "APPLIED"
    assert await _apply(store, _event("2", "PURCHASE", may, 500)) == "APPLIED"
    # a refund in September finds its March original in the March hash
    assert await _apply(store, _event("3", "REFUND", NOW, 400, original="t-1")) == "APPLIED"
    # the per-event state hash holds no ledger at all
    fields = await store.r.hkeys(state_key("C1"))
    assert fields and not any(f.startswith("t:") for f in fields)
    march_key = redis_store.txn_key("C1", march.strftime("%Y-%m"))
    assert (await store.r.hget(march_key, "t-1")).endswith("|1000|400|0")
    # each month's hash leaves on its own once past retention
    left = redis_store.month_ttl(march.strftime("%Y-%m"), NOW)
    assert left - 5 <= await store.r.ttl(march_key) <= left
    # a replayed purchase is still a duplicate, found by id in its month
    _, _, state = await store.admit("C1", "e-1b", ["t-1"], as_of=NOW)
    assert apply_event(state, _event("1", "PURCHASE", march, 1000).model_copy(
        update={"eventId": "e-1b"}), now=NOW) == "DUPLICATE_TRANSACTION"


async def test_a_month_in_retention_on_the_callers_clock_is_kept(store):
    """Retention runs on the processor's clock, not the wall clock: replaying a dataset
    with an older `now` (tests, `rebuild_state.py --now`) must keep the months it needs.
    The month's key used to get an absolute expiry that could already be past."""
    then = datetime.now(UTC) - timedelta(days=200)
    env = _event("1", "PURCHASE", then - timedelta(days=170), 1000)
    _, _, state = await store.admit("C1", env.eventId, ["t-1"], as_of=then)
    assert apply_event(state, env, now=then) == "APPLIED"
    await store.save(state, then)
    month = (then - timedelta(days=170)).strftime("%Y-%m")
    assert await store.r.exists(redis_store.txn_key("C1", month))


async def test_an_original_past_retention_is_absent_as_prune_would_have_it(store):
    old = NOW - timedelta(days=185)
    state = CustomerState(customerId="C1")
    state.txns["t-old"] = TxnRecord(old.date().isoformat(), "FOOD", "M1", 1000)
    state.touch_txn("t-old")
    await store.save(state, old)  # written while still in retention
    _, _, state = await store.admit("C1", "e-9", ["t-9", "t-old"], as_of=NOW)
    assert "t-old" not in state.txns


async def test_every_key_of_a_customer_shares_its_cluster_slot(store):
    await _apply(store, _event("1", "PURCHASE", NOW - timedelta(days=40), 1000))
    await store.put_cached("C1|JKT|ALL|10", {"x": 1})
    await store.record_served("C1", "req-1", "m", ["M1"])
    await store.load_tombstones(["C1"])
    keys = [k async for k in store.r.scan_iter(match="*{C1}*")]
    kinds = {k.split(":")[0].split("{")[0] for k in keys}
    assert kinds >= {"state", "txn", "rec", "recidx", "served", "erased"}, keys
    assert len({key_slot(k.encode()) for k in keys}) == 1, keys
    # no global per-event key: the tombstone is per customer
    assert await store.is_erased("C1") and not await store.is_erased("C2")
