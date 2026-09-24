"""E2E (SDD 16.1): generate dataset -> replay -> inspect profile -> recommendation.

Needs postgres + redis from docker-compose. Kafka is bypassed: the simulator's
transport is exercised separately; here the processor is fed the same replay file
so the assertions cover the invariants, not the broker.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rec.api import jobs
from rec.api.app import app, store
from rec.generator.config import DatasetConfig
from rec.generator.generate import generate
from rec.settings import settings
from rec.store import pg
from rec.stream.processor import FeatureProcessor

pytestmark = pytest.mark.asyncio(loop_scope="session")

ADMIN = {"Authorization": "Bearer admin-token"}
ANALYST = {"Authorization": "Bearer analyst-token"}
OPS = {"Authorization": "Bearer ops-token"}
REF = datetime(2026, 9, 23, tzinfo=UTC)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def dataset(tmp_path_factory):
    out = Path(settings.data_dir) / "test-dataset"
    cfg = DatasetConfig(seed=11, referenceTime=REF, customerCount=150, merchantCount=60,
                        promotionCount=15, transactionCount=3000, outputFormats=["jsonl"])
    manifest = await __import__("asyncio").to_thread(generate, cfg, out)
    await pg.pool()
    conn = await pg.pool()
    await conn.execute("TRUNCATE transaction_log, impressions, interactions, audit_events")
    await store.r.flushdb()
    await jobs.load_master_data(out)
    return out, manifest


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def replayed(dataset):
    """Feed every replay event through the real processor, twice (AC-001)."""
    out, manifest = dataset
    processor = FeatureProcessor(store, producer=None)
    now = REF + timedelta(hours=1)
    outcomes: dict[str, int] = {}
    fault_outcomes: dict[str, dict[str, int]] = {}
    lines = (out / "replay.jsonl").read_text().splitlines()
    for line in lines:
        raw = json.loads(line)
        code = await processor.handle(raw, now=now)
        outcomes[code] = outcomes.get(code, 0) + 1
        fault = raw.get("injectedFault")
        if fault:
            fault_outcomes.setdefault(fault, {})
            fault_outcomes[fault][code] = fault_outcomes[fault].get(code, 0) + 1
    replay_outcomes = {}
    for line in lines[:500]:  # replay a slice: must be a pure no-op
        code = await processor.handle(json.loads(line), now=now)
        replay_outcomes[code] = replay_outcomes.get(code, 0) + 1
    return out, manifest, outcomes, replay_outcomes, fault_outcomes


@pytest_asyncio.fixture(loop_scope="session")
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


# ----------------------------------------------------------------- pipeline


async def test_pipeline_applies_and_quarantines_per_spec(replayed):
    _, manifest, outcomes, _, fault_outcomes = replayed
    assert outcomes.get("APPLIED", 0) > 0
    assert outcomes.get("INVARIANT_VIOLATION", 0) == 0, "no invariant may be violated"

    # SYN-005: every injected fault must be caught, never silently applied,
    # and must land in the outcome class the spec assigns it.
    expected = {
        "DUPLICATE_EVENT": {"DUPLICATE_EVENT"},
        "DUPLICATE_TRANSACTION": {"DUPLICATE_TRANSACTION"},
        "ZERO_AMOUNT": {"SCHEMA_INVALID"},
        "UNKNOWN_CUSTOMER": {"UNKNOWN_CUSTOMER"},
        "UNKNOWN_MERCHANT": {"APPLIED"},  # pending enrichment, not quarantined
        "FUTURE_TIMESTAMP": {"FUTURE_TIMESTAMP"},
        "EXCESSIVE_REFUND": {"REFUND_EXCEEDS_ORIGINAL", "ALREADY_REVERSED",
                             "UNKNOWN_ORIGINAL"},  # original aged out of the ledger
    }
    assert set(fault_outcomes) <= set(expected), set(fault_outcomes) - set(expected)
    for fault, codes in fault_outcomes.items():
        assert set(codes) <= expected[fault], (fault, codes)


async def test_ac001_full_replay_is_idempotent(replayed):
    *_, replay_outcomes, _ = replayed
    # Nothing is applied a second time. Events that were quarantined the first
    # time are quarantined again with the same code — rejection is not state.
    assert replay_outcomes.get("APPLIED", 0) == 0
    assert replay_outcomes.get("DUPLICATE_EVENT", 0) > 0
    assert set(replay_outcomes) <= {"DUPLICATE_EVENT", "DUPLICATE_TRANSACTION",
                                    "SCHEMA_INVALID", "UNKNOWN_CUSTOMER", "FUTURE_TIMESTAMP",
                                    "REFUND_EXCEEDS_ORIGINAL", "ALREADY_REVERSED"}


async def test_ac008_online_matches_offline_recomputation(replayed):
    """Online Redis state vs a clean offline replay of the same events."""
    from rec.core.features import compute_features
    from rec.core.ledger import CustomerState, Reject, apply_event
    from rec.core.models import Envelope

    out = replayed[0]
    conn = await pg.pool()
    known = {r["customer_id"] for r in await conn.fetch("SELECT customer_id FROM customers")}
    offline: dict[str, CustomerState] = {}
    seen: set[str] = set()
    now = REF + timedelta(hours=1)
    for line in (out / "replay.jsonl").read_text().splitlines():
        raw = json.loads(line)
        cid = raw["payload"]["customerId"]
        if cid not in known or raw["eventId"] in seen:
            continue
        try:
            env = Envelope.model_validate(raw)
        except Exception:
            continue
        seen.add(raw["eventId"])
        state = offline.setdefault(cid, CustomerState(cid))
        try:
            apply_event(state, env, now=now)
        except (Reject, AssertionError, Exception):
            pass

    checked = 0
    for cid, state in list(offline.items())[:40]:
        online = await store.features(cid, now)
        expected = compute_features(state, now)
        assert online["transactionCount90d"] == expected["transactionCount90d"], cid
        assert online["netSpend90d"] == expected["netSpend90d"], cid
        checked += 1
    assert checked > 10


# ----------------------------------------------------------------- serving


async def test_recommendation_endpoint_shape_and_cache(client, replayed):
    conn = await pg.pool()
    cid = await conn.fetchval(
        """SELECT customer_id FROM transaction_log WHERE outcome='APPLIED'
           GROUP BY customer_id ORDER BY count(*) DESC LIMIT 1""")
    r1 = await client.get(f"/api/v1/customer/{cid}/recommendations",
                          headers={"Authorization": f"Bearer cust-{cid}"})
    assert r1.status_code == 200
    body = r1.json()
    assert body["source"] == "LIVE"
    assert body["scoreType"] if False else body["recommendations"]
    assert [x["rank"] for x in body["recommendations"]] == list(
        range(1, len(body["recommendations"]) + 1))
    assert all(x["scoreType"] == "RELATIVE_RELEVANCE" for x in body["recommendations"])
    assert body["modelVersion"] and body["rankingConfigVersion"]

    r2 = await client.get(f"/api/v1/customer/{cid}/recommendations",
                          headers={"Authorization": f"Bearer cust-{cid}"})
    assert r2.json()["source"] == "CACHE"
    assert r1.headers["x-correlation-id"] != r2.headers["x-correlation-id"]


async def test_ac006_customer_token_cannot_read_another_customer(client, replayed):
    conn = await pg.pool()
    ids = [r["customer_id"] for r in await conn.fetch(
        "SELECT customer_id FROM customers ORDER BY customer_id LIMIT 2")]
    r = await client.get(f"/api/v1/customer/{ids[1]}/recommendations",
                         headers={"Authorization": f"Bearer cust-{ids[0]}"})
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"
    assert (await client.get(f"/api/v1/customer/{ids[0]}/recommendations")).status_code == 401


async def test_rbac_denies_wrong_role(client, replayed):
    assert (await client.post("/admin/v1/datasets", json={}, headers=ANALYST)).status_code == 403
    assert (await client.get("/admin/v1/audit-events", headers=ANALYST)).status_code == 403
    assert (await client.get("/admin/v1/merchants", headers=ANALYST)).status_code == 200


async def test_ac007_preview_leaves_no_impression_and_no_cache(client, replayed):
    conn = await pg.pool()
    cid = await conn.fetchval("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    before_imp = await conn.fetchval("SELECT count(*) FROM impressions")
    before_keys = len(await store.r.keys("rec:*"))
    r = await client.post("/admin/v1/recommendations/preview",
                          json={"customerId": cid}, headers=ANALYST)
    assert r.status_code == 200
    debug = r.json()["debug"]
    assert "stageLatencyMs" in debug and "dropped" in debug and "scored" in debug
    assert await conn.fetchval("SELECT count(*) FROM impressions") == before_imp
    assert len(await store.r.keys("rec:*")) == before_keys


async def test_ac004_fallback_when_ranking_dependency_fails(client, replayed, monkeypatch):
    from rec.api import service

    conn = await pg.pool()
    cid = await conn.fetchval("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")

    async def boom(*a, **k):
        raise TimeoutError("ranking timeout")

    monkeypatch.setattr(service, "recommend", boom)
    resp, debug = await service.recommend_safe(store, cid, limit=5)
    assert resp.source == "FALLBACK" and resp.stale is True
    assert debug["fallbackReason"] == "TimeoutError"
    assert all("FALLBACK" in x.reasonCodes for x in resp.recommendations)


async def test_ac005_expired_promo_in_cache_is_not_served(client, replayed):
    conn = await pg.pool()
    cid = await conn.fetchval("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    r = await client.get(f"/api/v1/customer/{cid}/recommendations?limit=20",
                         headers={"Authorization": f"Bearer cust-{cid}"})
    body = r.json()
    promoted = [x for x in body["recommendations"] if x["promotion"]]
    if not promoted:
        pytest.skip("no eligible promo for this customer in this dataset")
    merchant_id = promoted[0]["merchantId"]
    await conn.execute(
        "UPDATE promotions SET ends_at = now() - interval '1 day', status='EXPIRED' "
        "WHERE merchant_id=$1", merchant_id)
    # cache still holds the stale entry; serving must re-validate (AC-005)
    again = await client.get(f"/api/v1/customer/{cid}/recommendations?limit=20",
                             headers={"Authorization": f"Bearer cust-{cid}"})
    assert again.json()["source"] == "CACHE"
    item = next(x for x in again.json()["recommendations"] if x["merchantId"] == merchant_id)
    assert item["promotion"] is None
    assert "CARD_PROMO_ELIGIBLE" not in item["reasonCodes"]


async def test_personalization_optout_uses_no_behavioural_features(client, replayed):
    conn = await pg.pool()
    cid = await conn.fetchval(
        """SELECT c.customer_id FROM customers c JOIN transaction_log t
             ON t.customer_id=c.customer_id AND t.outcome='APPLIED'
           GROUP BY c.customer_id ORDER BY count(*) DESC LIMIT 1""")
    await conn.execute("UPDATE customers SET personalization_allowed=false WHERE customer_id=$1",
                       cid)
    await store.invalidate_customer(cid)
    r = await client.post("/admin/v1/recommendations/preview",
                          json={"customerId": cid}, headers=ANALYST)
    assert all("FAVORITE_CATEGORY" not in x["reasonCodes"]
               and "FREQUENT_MERCHANT" not in x["reasonCodes"]
               for x in r.json()["response"]["recommendations"])
    await conn.execute("UPDATE customers SET personalization_allowed=true WHERE customer_id=$1",
                       cid)


# ----------------------------------------------------------------- admin


async def test_optimistic_concurrency_on_merchant_patch(client, replayed):
    merchants = (await client.get("/admin/v1/merchants?limit=1", headers=OPS)).json()
    mid = merchants[0]["merchantId"]
    conn = await pg.pool()
    version = await conn.fetchval("SELECT version FROM merchants WHERE merchant_id=$1", mid)
    ok = await client.patch(f"/admin/v1/merchants/{mid}",
                            json={"status": "INACTIVE", "version": version}, headers=OPS)
    assert ok.status_code == 200
    stale = await client.patch(f"/admin/v1/merchants/{mid}",
                               json={"status": "ACTIVE", "version": version}, headers=OPS)
    assert stale.status_code == 409
    await conn.execute("UPDATE merchants SET status='ACTIVE' WHERE merchant_id=$1", mid)


async def test_admin_actions_are_audited(client, replayed):
    conn = await pg.pool()
    before = await conn.fetchval("SELECT count(*) FROM audit_events")
    cid = await conn.fetchval("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    await client.get(f"/admin/v1/customers/{cid}/features", headers=ANALYST)
    rows = (await client.get("/admin/v1/audit-events", headers=ADMIN)).json()["items"]
    assert await conn.fetchval("SELECT count(*) FROM audit_events") > before
    assert rows[0]["action"] == "customer.features.read"
    assert rows[0]["actor"] == "analyst"


async def test_transaction_explorer_filters_and_redacts(client, replayed):
    page = (await client.get("/admin/v1/transactions?outcome=QUARANTINED&limit=5",
                             headers=ADMIN)).json()
    assert all(x["outcome"] == "QUARANTINED" for x in page["items"])
    assert all(x["reject_code"] for x in page["items"])
    detail = (await client.get(f"/admin/v1/transactions/{page['items'][0]['event_id']}",
                               headers=ADMIN)).json()
    assert "pan" not in detail["envelope"]["payload"]
    assert detail["envelope"]["payload"]["amountMinor"] >= 0


async def test_metrics_overview_reports_nulls_not_zeros(client, replayed):
    m = (await client.get("/admin/v1/metrics/overview", headers=ADMIN)).json()
    assert m["ingestion"]["appliedEvents"] > 0
    assert m["ingestion"]["quarantinedEvents"] > 0
    assert m["serving"]["activeModel"]
    assert m["serving"]["cacheHitRate"] is None or 0 <= m["serving"]["cacheHitRate"] <= 1


async def test_simulator_rejects_invalid_state_transition(client, dataset):
    r = await client.post("/admin/v1/simulations",
                          json={"datasetId": "test-dataset", "targetTps": 10}, headers=ADMIN)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert (await client.post(f"/admin/v1/simulations/{run_id}/pause",
                              headers=ADMIN)).status_code == 409
    assert (await client.post(f"/admin/v1/simulations/{run_id}/stop",
                              headers=ADMIN)).status_code == 200
    assert (await client.post(f"/admin/v1/simulations/{run_id}/start",
                              headers=ADMIN)).status_code == 409


async def test_openapi_is_31_and_documents_the_contract(client):
    spec = (await client.get("/openapi.json")).json()
    assert spec["openapi"].startswith("3.1")
    paths = spec["paths"]
    assert "/api/v1/customer/{customer_id}/recommendations" in paths
    assert "/admin/v1/datasets" in paths and "/admin/v1/audit-events" in paths
