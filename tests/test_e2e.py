"""E2E (SDD 16.1): generate dataset -> replay -> inspect profile -> recommendation.

Needs postgres + redis from docker-compose. Kafka is bypassed: the simulator's
transport is exercised separately; here the processor is fed the same replay file
so the assertions cover the invariants, not the broker.
"""
from __future__ import annotations

import asyncio
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
    await conn.execute("TRUNCATE transaction_log, impressions, interactions, audit_events, "
                       "erasure_requests, erased_customers")
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


# ===================================================================== Fase 4: ML serving


ML = {"Authorization": "Bearer ml-token"}
APPROVER = {"Authorization": "Bearer approver-token"}


async def _register_fake_model(model_version: str, *, approved: bool = True,
                               schema: str = None) -> None:
    """Insert a model row directly: these tests exercise the serving and promotion
    decisions, not XGBoost. Training itself is covered in tests/test_ml_pipeline.py."""
    from rec.ml import registry
    from rec.ml.vectorize import FEATURE_SCHEMA_VERSION as VECTOR_SCHEMA_VERSION

    gates = [{"name": "ndcg_not_worse_than_baseline", "passed": approved, "detail": "test"}]
    await registry.record_model({
        "modelVersion": model_version,
        "featureSchemaVersion": schema or VECTOR_SCHEMA_VERSION,
        "trainerVersion": "test",
        "artifacts": {"model": f"/tmp/{model_version}.json"},
        "mlflowRunId": None,
        "approved": approved,
        "metrics": {"ndcg@10": 0.6}, "baselineMetrics": {"ndcg@10": 0.5},
        "segmentMetrics": {}, "gates": gates, "datasetLineage": {},
    }, dataset_id="test-dataset", job_id=None)


@pytest_asyncio.fixture(loop_scope="session")
async def baseline_deployment():
    """Every ML test starts and ends on the baseline so ordering cannot matter."""
    conn = await pg.pool()
    await conn.execute("""UPDATE model_deployment SET mode='BASELINE', model_version=NULL,
                          previous_version=NULL, canary_percent=0 WHERE id=1""")
    yield
    await conn.execute("""UPDATE model_deployment SET mode='BASELINE', model_version=NULL,
                          previous_version=NULL, canary_percent=0 WHERE id=1""")


async def _no_warm(monkeypatch):
    from rec.ml import client as ranking_client

    async def warm(_version, **_kwargs):
        return None

    monkeypatch.setattr(ranking_client, "warm", warm)


async def test_promotion_requires_approver_and_passing_gates(client, replayed,
                                                             baseline_deployment, monkeypatch):
    await _no_warm(monkeypatch)
    await _register_fake_model("m-good")
    await _register_fake_model("m-failed-gates", approved=False)

    # separation of duties: the ML Engineer who trains does not promote
    assert (await client.post("/admin/v1/models/m-good/promote", json={"mode": "FULL"},
                              headers=ML)).status_code == 403
    refused = await client.post("/admin/v1/models/m-failed-gates/promote",
                                json={"mode": "FULL"}, headers=APPROVER)
    assert refused.status_code == 409
    assert "gates" in refused.json()["message"]
    missing = await client.post("/admin/v1/models/does-not-exist/promote",
                                json={"mode": "FULL"}, headers=APPROVER)
    assert missing.status_code == 409

    ok = await client.post("/admin/v1/models/m-good/promote",
                           json={"mode": "FULL"}, headers=APPROVER)
    assert ok.status_code == 200 and ok.json()["mode"] == "FULL"


async def test_feature_schema_mismatch_blocks_promotion(client, replayed,
                                                        baseline_deployment, monkeypatch):
    await _no_warm(monkeypatch)
    await _register_fake_model("m-old-schema", schema="0.9.0")
    refused = await client.post("/admin/v1/models/m-old-schema/promote",
                                json={"mode": "FULL"}, headers=APPROVER)
    assert refused.status_code == 409
    assert "feature schema" in refused.json()["message"]


async def test_model_ranks_when_promoted_and_degrades_when_it_fails(client, replayed,
                                                                   baseline_deployment,
                                                                   monkeypatch):
    from rec.api import service
    from rec.ml import client as ranking_client
    from rec.ml import registry

    conn = await pg.pool()
    cid = await conn.fetchval(
        """SELECT customer_id FROM transaction_log WHERE outcome='APPLIED'
           GROUP BY customer_id ORDER BY count(*) DESC LIMIT 1""")
    await _register_fake_model("m-live")
    await registry.promote("m-live", mode="FULL", canary_percent=100, actor="test")

    # a model that simply reverses the baseline order proves the model output is used
    async def reversed_scores(_version, candidates):
        return [float(-i) for i in range(len(candidates))], 1.23

    monkeypatch.setattr(ranking_client, "score", reversed_scores)
    response, debug = await service.recommend(store, cid, use_cache=False, explain=True)
    assert response.modelVersion == "m-live" and response.source == "LIVE"
    assert debug["deployment"]["ranker"] == "MODEL"
    assert debug["inferenceMs"] == 1.23
    assert all(isinstance(item.reasonCodes, list) for item in response.recommendations)

    async def boom(_version, _candidates):
        raise ranking_client.RankingUnavailable("RANKING_TIMEOUT")

    monkeypatch.setattr(ranking_client, "score", boom)
    degraded, debug2 = await service.recommend(store, cid, use_cache=False, explain=True)
    assert degraded.source == "FALLBACK"
    assert degraded.modelVersion == registry.BASELINE_VERSION
    assert debug2["degradeReason"] == "RANKING_TIMEOUT"
    assert len(degraded.recommendations) > 0, "a ranking failure must not empty the response"


async def test_shadow_mode_does_not_change_what_is_served(client, replayed,
                                                          baseline_deployment, monkeypatch):
    from rec.api import service
    from rec.ml import client as ranking_client
    from rec.ml import registry

    conn = await pg.pool()
    cid = await conn.fetchval("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    served_baseline, _ = await service.recommend(store, cid, use_cache=False)

    await _register_fake_model("m-shadow")
    await registry.promote("m-shadow", mode="SHADOW", canary_percent=0, actor="test")

    async def reversed_scores(_version, candidates):
        return [float(-i) for i in range(len(candidates))], 5.0

    monkeypatch.setattr(ranking_client, "score", reversed_scores)
    before = await conn.fetchval("SELECT count(*) FROM shadow_evaluations")
    shadowed, debug = await service.recommend(store, cid, use_cache=False)
    assert shadowed.modelVersion == registry.BASELINE_VERSION
    assert [r.merchantId for r in shadowed.recommendations] == \
           [r.merchantId for r in served_baseline.recommendations]
    assert "shadow" in debug
    await asyncio.sleep(0.8)  # fire-and-forget comparison
    assert await conn.fetchval("SELECT count(*) FROM shadow_evaluations") > before
    row = await conn.fetchrow(
        "SELECT * FROM shadow_evaluations ORDER BY occurred_at DESC LIMIT 1")
    assert row["model_version"] == "m-shadow" and row["served_source"] == "BASELINE"


async def test_canary_routes_only_its_share_and_is_stable_per_customer(client, replayed,
                                                                      baseline_deployment,
                                                                      monkeypatch):
    from rec.api import service
    from rec.ml import client as ranking_client
    from rec.ml import registry

    async def scores(_version, candidates):
        return [1.0] * len(candidates), 1.0

    monkeypatch.setattr(ranking_client, "score", scores)
    await _register_fake_model("m-canary")
    await registry.promote("m-canary", mode="CANARY", canary_percent=50, actor="test")

    conn = await pg.pool()
    ids = [r["customer_id"] for r in await conn.fetch(
        "SELECT customer_id FROM customers ORDER BY customer_id LIMIT 24")]
    versions = {}
    for cid in ids:
        response, _ = await service.recommend(store, cid, use_cache=False)
        versions[cid] = response.modelVersion
    assert set(versions.values()) == {"m-canary", registry.BASELINE_VERSION}, versions
    again, _ = await service.recommend(store, ids[0], use_cache=False)
    assert again.modelVersion == versions[ids[0]], "a customer must not flip between rankers"


async def test_cache_key_includes_the_model_version():
    from rec.api.service import cache_key

    assert cache_key("C1", "JKT", None, 10, "m-a") != cache_key("C1", "JKT", None, 10, "m-b")


async def test_rollback_returns_to_previous_then_to_baseline(client, replayed,
                                                             baseline_deployment, monkeypatch):
    await _no_warm(monkeypatch)
    await _register_fake_model("m-v1")
    await _register_fake_model("m-v2")
    await client.post("/admin/v1/models/m-v1/promote", json={"mode": "FULL"}, headers=APPROVER)
    await client.post("/admin/v1/models/m-v2/promote", json={"mode": "FULL"}, headers=APPROVER)

    first = await client.post("/admin/v1/models/m-v2/rollback", headers=APPROVER)
    assert first.json()["model_version"] == "m-v1"
    second = await client.post("/admin/v1/models/m-v1/rollback", headers=APPROVER)
    assert second.json()["mode"] == "BASELINE" and second.json()["model_version"] is None
    assert (await client.post("/admin/v1/models/m-v1/rollback",
                              headers=ANALYST)).status_code == 403


async def test_training_job_endpoint_guards_role_and_dataset(client, replayed):
    assert (await client.post("/admin/v1/training-jobs",
                              json={"datasetId": "test-dataset"},
                              headers=ANALYST)).status_code == 403
    missing = await client.post("/admin/v1/training-jobs",
                                json={"datasetId": "no-such-dataset"}, headers=ML)
    assert missing.status_code == 404


async def test_model_endpoints_are_audited(client, replayed, baseline_deployment, monkeypatch):
    await _no_warm(monkeypatch)
    await _register_fake_model("m-audited")
    await client.post("/admin/v1/models/m-audited/promote", json={"mode": "SHADOW"},
                      headers=APPROVER)
    rows = (await client.get("/admin/v1/audit-events", headers=ADMIN)).json()["items"]
    entry = next(r for r in rows if r["action"] == "model.promote")
    assert entry["actor"] == "approver" and "m-audited" in entry["resource"]


# ----------------------------------------------------------------- Fase 5: hardening


async def test_guardrail_rolls_back_a_failing_live_model(client, replayed, baseline_deployment,
                                                         monkeypatch):
    """SDD 17.2 step 14: a promoted model that keeps failing is rolled back without a human."""
    from rec.api import service
    from rec.ml import client as ranking_client
    from rec.ml import guardrail, registry

    monkeypatch.setattr(settings, "guardrail_min_requests", 5)
    conn = await pg.pool()
    cid = await conn.fetchval("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    await _register_fake_model("m-guarded")
    await registry.promote("m-guarded", mode="FULL", canary_percent=100, actor="test")

    async def healthy(_version, candidates):
        return [0.0] * len(candidates), 1.0

    monkeypatch.setattr(ranking_client, "score", healthy)
    for _ in range(5):
        await service.recommend(store, cid, use_cache=False)
    assert await guardrail.check(store) is None, "a healthy model must stay live"

    async def boom(_version, _candidates):
        raise ranking_client.RankingUnavailable("RANKING_TIMEOUT")

    monkeypatch.setattr(ranking_client, "score", boom)
    for _ in range(5):
        await service.recommend(store, cid, use_cache=False)
    await store.r.delete(guardrail.LOCK_KEY)
    rolled = await guardrail.check(store)
    assert rolled is not None and rolled["mode"] == "BASELINE"
    rows = (await client.get("/admin/v1/audit-events", headers=ADMIN)).json()["items"]
    entry = next(r for r in rows if r["actor"] == "system:guardrail")
    assert entry["outcome"] == "AUTOMATIC" and entry["changes"]["breaches"]


async def test_redis_loss_degrades_to_popular_not_to_an_error(replayed):
    """SDD 16 resilience: online store down -> SERV-003 popular list, still HTTP 200."""
    import redis.asyncio as aioredis

    from rec.api import service
    from rec.store.redis_store import OnlineStore

    dead = OnlineStore(aioredis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2))
    conn = await pg.pool()
    cid = await conn.fetchval("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
    response, debug = await service.recommend_safe(dead, cid)
    assert response.source == "FALLBACK" and response.recommendations
    assert debug["fallbackReason"]
    await dead.close()


async def test_audit_log_cannot_be_rewritten(replayed):
    conn = await pg.pool()
    await pg.audit("t", "Auditor", "test.append", "x")
    with pytest.raises(Exception, match="append-only"):
        await conn.execute("UPDATE audit_events SET actor='someone-else'")
    with pytest.raises(Exception, match="append-only"):
        await conn.execute("DELETE FROM audit_events")


async def test_ac009_erased_customer_is_not_rematerialised_by_replay(client, replayed):
    """AC-009: approved erasure removes the customer everywhere; replaying the archive
    afterwards brings nothing back."""
    out, *_ = replayed
    conn = await pg.pool()
    cid = await conn.fetchval(
        """SELECT customer_id FROM transaction_log WHERE outcome='APPLIED'
           GROUP BY customer_id ORDER BY count(*) DESC LIMIT 1""")
    assert await store.r.exists(f"state:{cid}")

    filed = await client.post("/admin/v1/erasure-requests",
                              json={"customerId": cid, "reason": "PDP deletion request"},
                              headers=ADMIN)
    assert filed.status_code == 201
    rid = filed.json()["requestId"]
    # maker-checker: the operator cannot approve; the approver can
    assert (await client.post(f"/admin/v1/erasure-requests/{rid}/decision",
                              json={"approve": True}, headers=ADMIN)).status_code == 403
    done = await client.post(f"/admin/v1/erasure-requests/{rid}/decision",
                             json={"approve": True}, headers=APPROVER)
    assert done.status_code == 200 and done.json()["deleted"]["transaction_log"] > 0

    # replay the whole archive, as after a disaster or a reprocessing run
    processor = FeatureProcessor(store, producer=None)
    outcomes = set()
    for line in (out / "replay.jsonl").read_text().splitlines():
        raw = json.loads(line)
        if raw.get("payload", {}).get("customerId") == cid:
            processor_outcome = await processor.handle(raw, now=REF + timedelta(hours=2))
            outcomes.add(processor_outcome)
    assert outcomes == {"ERASED_CUSTOMER"}
    assert not await store.r.exists(f"state:{cid}")
    assert await conn.fetchval("SELECT count(*) FROM transaction_log WHERE customer_id=$1",
                               cid) == 0
    # master-data reload does not resurrect the profile either
    await jobs.load_master_data(out)
    assert await pg.customer(cid) is None
    r = await client.get(f"/api/v1/customer/{cid}/recommendations",
                         headers={"Authorization": f"Bearer cust-{cid}"})
    assert r.status_code == 404


async def test_redis_state_rebuilds_exactly_from_postgres(replayed):
    """SDD 16 recovery: lose Redis entirely, rebuild from the log, get identical features."""
    import subprocess
    import sys

    conn = await pg.pool()
    ids = [r["customer_id"] for r in await conn.fetch(
        """SELECT customer_id FROM transaction_log WHERE outcome='APPLIED'
           GROUP BY customer_id ORDER BY count(*) DESC LIMIT 20""")]
    as_of = REF + timedelta(hours=3)
    before = {cid: await store.features(cid, as_of) for cid in ids}
    erased = await pg.erased_customers()

    done = await asyncio.to_thread(
        subprocess.run, [sys.executable, "scripts/rebuild_state.py", "--flush",
                          "--now", (REF + timedelta(hours=1)).isoformat()],
        capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]

    after = {cid: await store.features(cid, as_of) for cid in ids}
    for cid in ids:
        for key in ("transactionCount90d", "netSpend90d", "categoryInterest"):
            assert before[cid][key] == after[cid][key], (cid, key)
    for cid in erased:
        assert not await store.r.exists(f"state:{cid}"), "rebuild resurrected an erased customer"


async def test_simulator_never_skips_an_event_when_the_broker_fails(client, dataset,
                                                                    monkeypatch):
    """SIM-002 under broker loss: retry, then FAIL with the checkpoint on the unsent
    event; resume continues from there, so nothing is lost."""
    from rec.simulator import runner

    sent: list[bytes] = []
    state = {"down": True}

    class FlakyProducer:
        def __init__(self, **_kw):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

        async def send_and_wait(self, _topic, value, key=None):
            if state["down"] and len(sent) == 5:
                raise ConnectionError("broker down")
            sent.append(value)

    monkeypatch.setattr(runner, "AIOKafkaProducer", FlakyProducer)
    monkeypatch.setattr(runner, "SEND_ATTEMPTS", 2)
    run = (await client.post("/admin/v1/simulations", headers=ADMIN,
                             json={"datasetId": "test-dataset", "targetTps": 5000})).json()
    rid = run["run_id"]
    await client.post(f"/admin/v1/simulations/{rid}/start", headers=ADMIN)
    for _ in range(100):
        status_ = (await client.get(f"/admin/v1/simulations/{rid}", headers=ADMIN)).json()
        if status_["status"] == "FAILED":
            break
        await asyncio.sleep(0.1)
    assert status_["status"] == "FAILED" and status_["offset_pos"] == 5

    state["down"] = False
    await client.post(f"/admin/v1/simulations/{rid}/resume", headers=ADMIN)
    for _ in range(300):
        status_ = (await client.get(f"/admin/v1/simulations/{rid}", headers=ADMIN)).json()
        if status_["status"] == "COMPLETED":
            break
        await asyncio.sleep(0.1)
    assert status_["status"] == "COMPLETED"
    replay = (dataset[0] / "replay.jsonl").read_text().splitlines()
    assert [v.decode().rstrip("\n") for v in sent] == replay, "every event once, in order"
