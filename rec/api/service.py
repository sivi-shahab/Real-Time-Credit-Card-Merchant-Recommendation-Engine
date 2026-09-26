"""SERV-001..004 — recommendation orchestration, cache, fallback.

Ranking is either the deterministic baseline (ML-001) or the promoted XGBoost model
(ML-002), decided by the deployment row. Any model failure degrades to the baseline
rather than to an error (SERV-003).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime

from rec.core.eligibility import evaluate, offer_view
from rec.core.models import (
    FEATURE_SCHEMA_VERSION,
    RANKING_CONFIG_VERSION,
    CardTier,
    Customer,
    RecommendationResponse,
)
from rec.core.ranking import (
    DEFAULT_RESULTS,
    MAX_RESULTS,
    diversify,
    generate_candidates,
    score_baseline,
    to_recommendations,
)
from rec.ml import bandit, guardrail, registry, uplift
from rec.ml import client as ranking_client
from rec.ml.vectorize import vectorize
from rec.obs import RANKING_DEGRADED, RECOMMENDATIONS, redact
from rec.settings import settings
from rec.store import pg
from rec.store.redis_store import OnlineStore

log = logging.getLogger("serving")


class Stage:
    """Per-stage latency for UI-006."""

    def __init__(self):
        self.timings: dict[str, float] = {}

    def __call__(self, name: str):
        outer = self

        class _Ctx:
            def __enter__(self_):
                self_.t0 = time.perf_counter()
                return self_

            def __exit__(self_, *exc):
                outer.timings[name] = round((time.perf_counter() - self_.t0) * 1000, 2)
                return False

        return _Ctx()


def cache_key(customer_id: str, city: str, channel: str | None, limit: int,
              model_version: str) -> str:
    """SERV-004 — context, model version and ranking config are all part of the key, so
    promoting a model cannot serve stale entries ranked by the previous one."""
    return (f"{customer_id}|{city}|{channel or 'ANY'}|{limit}"
            f"|{model_version}|{RANKING_CONFIG_VERSION}|{FEATURE_SCHEMA_VERSION}")


def _ranker_for(deployment: dict, customer_id: str) -> tuple[str, str]:
    """Returns (ranker, modelVersion) where ranker is BASELINE or MODEL."""
    mode = deployment.get("mode", "BASELINE")
    model_version = deployment.get("model_version")
    if not model_version or mode in ("BASELINE", "SHADOW"):
        return "BASELINE", registry.BASELINE_VERSION
    if mode == "CANARY" and not registry.in_canary(
            customer_id, int(deployment.get("canary_percent") or 0)):
        return "BASELINE", registry.BASELINE_VERSION
    return "MODEL", model_version


def _vectors(candidates, features: dict, *, city_code: str, eligible: dict,
             now: datetime) -> list[dict[str, float]]:
    return [
        vectorize(features, merchant_id=m.merchantId, merchant_rating=m.rating,
                  merchant_channel=m.channel.value, merchant_category=m.categoryCode,
                  merchant_city=m.cityCode, city_code=city_code,
                  promo=eligible.get(m.merchantId), request_time=now)
        for m in candidates
    ]


async def _run_shadow(store: OnlineStore, model_version: str, sha256: str | None,
                      request_id: str,
                      customer_id: str, vectors: list[dict[str, float]],
                      candidate_ids: list[str], served_order: list[str],
                      limit: int) -> None:
    """Score the candidate set with the shadow model and record the disagreement.

    Errors are swallowed on purpose: a shadow evaluation must never affect the customer
    response or surface as a request failure.
    """
    try:
        scores, inference_ms = await ranking_client.score(model_version, vectors,
                                                          sha256=sha256)
        ranked = [merchant_id for _, merchant_id in
                  sorted(zip(scores, candidate_ids, strict=True),
                         key=lambda pair: (-pair[0], pair[1]))][:limit]
        await registry.record_shadow(
            request_id, customer_id, model_version, "BASELINE",
            registry.rank_agreement(served_order, ranked),
            bool(served_order and ranked and served_order[0] == ranked[0]),
            inference_ms)
        await store.incr_metric("shadow_evaluations")
    except Exception as exc:  # noqa: BLE001 - shadow failures are informational only
        log.info("shadow evaluation skipped: %s", exc)
        await store.incr_metric("shadow_failures")


async def recommend(
    store: OnlineStore,
    customer_id: str,
    *,
    city_code: str | None = None,
    channel: str | None = None,
    limit: int = DEFAULT_RESULTS,
    use_cache: bool = True,
    preview: bool = False,
    explain: bool = False,
    now: datetime | None = None,
) -> tuple[RecommendationResponse, dict]:
    now = now or datetime.now(UTC)
    limit = max(1, min(limit, MAX_RESULTS))
    stage = Stage()
    debug: dict = {"dropped": [], "stageLatencyMs": stage.timings}

    row = await pg.customer(customer_id)
    if row is None:
        raise KeyError(customer_id)
    customer = Customer(
        customerId=row["customer_id"], cityCode=row["city_code"],
        cardTier=CardTier(row["card_tier"]),
        personalizationAllowed=row["personalization_allowed"],
    )
    city = city_code or customer.cityCode
    with stage("deployment"):
        deployment = await registry.deployment()
    ranker, model_version = _ranker_for(deployment, customer_id)
    debug["deployment"] = {"mode": deployment.get("mode"), "ranker": ranker,
                           "modelVersion": model_version,
                           "canaryPercent": deployment.get("canary_percent")}
    key = cache_key(customer_id, city, channel, limit, model_version)
    holdout = uplift.in_holdout(customer_id, settings.promo_holdout_percent)
    if holdout:  # ADR-0010: a holdout response carries no offers, so it caches apart
        key += "|PROMO_HOLDOUT"
        debug["promoHoldout"] = True

    if use_cache and not preview:
        with stage("cache"):
            cached = await store.get_cached(key)
        if cached:
            resp = RecommendationResponse.model_validate(cached)
            # AC-005: promos are re-validated on every serve, cache or not
            valid_ids = await pg.active_promotion_merchants(now)
            for item in resp.recommendations:
                if item.promotion and item.merchantId not in valid_ids:
                    item.promotion = None
                    item.reasonCodes = [r for r in item.reasonCodes
                                        if r != "CARD_PROMO_ELIGIBLE"]
            resp.source = "CACHE"
            await store.incr_metric("recommendation_cache_hit")
            return resp, debug
        await store.incr_metric("recommendation_cache_miss")

    with stage("features"):
        catalog, merchant_city, all_promos = await pg.catalog()
        if customer.personalizationAllowed:
            features = await store.features(customer_id, now, merchant_city)
        else:
            # CAND-003: no behavioural features when personalisation is refused
            features = {"categoryInterest": {}, "merchantAffinity": {}, "coldStartFlag": True,
                        "featureAsOf": now.isoformat(), "personalizationAllowed": False}

    with stage("catalog"):
        merchants = catalog
        # AC-005: the snapshot is current (versioned); the window is checked at `now`
        promos = pg.active_at(all_promos, now)
        redemptions = await pg.customer_redemption_counts(customer_id)

    by_merchant = {m.merchantId: m for m in merchants}
    with stage("promo"):
        eligible: dict[str, dict] = {}
        for promo in ([] if holdout else promos):
            merchant = by_merchant.get(promo.merchantId)
            if merchant is None:
                continue
            ok, reason = evaluate(promo, customer, merchant, now=now,
                                  customer_redemptions=redemptions.get(promo.merchantId, 0))
            if ok:
                eligible.setdefault(promo.merchantId, offer_view(promo))
            elif explain:
                debug.setdefault("promoRejections", []).append(
                    {"promotionId": promo.promotionId, "reason": reason})

    with stage("candidates"):
        candidates, dropped = generate_candidates(
            merchants, features, city_code=city, channel=channel,
            promo_merchant_ids=set(eligible),
        )
        debug["dropped"] = dropped
        debug["candidateCount"] = len(candidates)

    # Reason codes always come from the baseline components: they explain the offer to
    # the customer and must stay stable regardless of which ranker ordered the list.
    with stage("ranking"):
        scored = []
        for merchant in candidates:
            score, parts, reasons = score_baseline(
                merchant, features, city_code=city,
                promo_eligible=merchant.merchantId in eligible,
            )
            scored.append((score, merchant, {"reasonCodes": reasons, "components": parts}))
        scored.sort(key=lambda x: (-x[0], x[1].merchantId))
        baseline_order = [m.merchantId for _, m, _ in scored]
        effective_version = registry.BASELINE_VERSION
        degraded: str | None = None

        if ranker == "MODEL" and candidates:
            meta_by_merchant = {m.merchantId: meta for _, m, meta in scored}
            vectors = _vectors(candidates, features, city_code=city, eligible=eligible, now=now)
            t0 = time.perf_counter()
            try:
                scores, inference_ms = await ranking_client.score(
                    model_version, vectors, sha256=deployment.get("artifact_sha256"))
                scored = [
                    (float(score), merchant,
                     meta_by_merchant[merchant.merchantId] | {"ranker": "MODEL"})
                    for score, merchant in zip(scores, candidates, strict=True)
                ]
                scored.sort(key=lambda x: (-x[0], x[1].merchantId))
                effective_version = model_version
                debug["inferenceMs"] = inference_ms
                await store.incr_metric("ranking_model_scored")
            except ranking_client.RankingUnavailable as exc:
                degraded = exc.reason
                debug["degradedTo"] = "BASELINE"
                debug["degradeReason"] = exc.reason
                await store.incr_metric(f"ranking_degraded_{exc.reason}")
                await store.incr_metric("ranking_degraded")
                RANKING_DEGRADED.labels(exc.reason).inc()
                log.warning("ranking unavailable (%s); serving baseline", exc.reason)
            if not preview:
                await guardrail.record(store.r, deployment, degraded=degraded is not None,
                                       latency_ms=(time.perf_counter() - t0) * 1000)

        if explain:
            debug["scored"] = [
                {"merchantId": m.merchantId, "score": round(s, 6), **meta}
                for s, m, meta in scored[:50]
            ]
        top = diversify(scored, limit)

    # SHADOW: serve the baseline, compare what the model would have done. Fire-and-forget
    # so the shadow call cannot enter the customer-facing latency budget.
    shadow_version = deployment.get("model_version")
    if (deployment.get("mode") == "SHADOW" and shadow_version and candidates
            and not preview):
        request_id_for_shadow = str(uuid.uuid4())
        asyncio.create_task(_run_shadow(
            store, shadow_version, deployment.get("artifact_sha256"),
            request_id_for_shadow, customer_id,
            _vectors(candidates, features, city_code=city, eligible=eligible, now=now),
            [m.merchantId for m in candidates], baseline_order[:limit], limit))
        debug["shadow"] = {"modelVersion": shadow_version, "requestId": request_id_for_shadow}

    response = RecommendationResponse(
        requestId=str(uuid.uuid4()),
        customerId=customer_id,
        generatedAt=now,
        featureAsOf=features.get("featureAsOf"),
        modelVersion=effective_version,
        source="LIVE" if degraded is None else "FALLBACK",
        stale=bool(features.get("coldStartFlag")),
        recommendations=to_recommendations(top, eligible),
    )
    if not top:
        response.source = "FALLBACK"
        debug["fallbackReason"] = "NO_SAFE_CANDIDATES"

    if settings.promo_holdout_percent and not preview:
        asyncio.create_task(uplift.record_exposure(customer_id, holdout,
                                                   settings.promo_holdout_percent))

    # Online bandit (ADR-0007 stage 2): shadow only, keyed by the id clients send
    # impressions with. Vectors are built inside the task, off this request's latency.
    if settings.online_bandit_enabled and top and not preview:
        asyncio.create_task(bandit.shadow(
            store, response.requestId, customer_id,
            [item.merchantId for item in response.recommendations],
            [m.merchantId for m in candidates],
            lambda: _vectors(candidates, features, city_code=city, eligible=eligible, now=now)))

    if use_cache and not preview:
        with stage("cacheWrite"):
            await store.put_cached(key, response.model_dump(mode="json"))
    await store.incr_metric("recommendation_served")
    RECOMMENDATIONS.labels(response.source, response.modelVersion).inc()
    return response, debug


async def recommend_safe(store: OnlineStore, customer_id: str, **kwargs):
    """SERV-003 fallback chain. Never raises for a known customer.

    Model failures are already handled inside `recommend` (degrade to baseline); this
    outer net catches feature-store or catalog failures and serves popular merchants.
    """
    try:
        return await recommend(store, customer_id, **kwargs)
    except KeyError:
        raise
    except Exception as exc:  # dependency failure -> popular, still promo-validated
        log.warning("serving fallback to popular list: %s", type(exc).__name__)
        RECOMMENDATIONS.labels("FALLBACK", registry.BASELINE_VERSION).inc()
        try:  # the failed dependency may well be Redis itself
            await store.incr_metric("recommendation_fallback")
        except Exception:  # noqa: BLE001
            pass
        kwargs.pop("explain", None)
        now = kwargs.get("now") or datetime.now(UTC)
        try:
            catalog, _, _ = await pg.catalog()
        except Exception:  # noqa: BLE001 - SERV-003 step 4: no safe candidates -> empty
            catalog = []
        merchants = sorted(
            [m for m in catalog if m.status == "ACTIVE"],
            key=lambda m: (-m.rating, m.merchantId),
        )[: kwargs.get("limit", DEFAULT_RESULTS)]
        items = [(m.rating / 5.0, m, {"reasonCodes": ["POPULAR_PICK", "FALLBACK"]})
                 for m in merchants]
        return (
            RecommendationResponse(
                requestId=str(uuid.uuid4()), customerId=customer_id, generatedAt=now,
                featureAsOf=None, modelVersion=registry.BASELINE_VERSION, source="FALLBACK",
                stale=True, recommendations=to_recommendations(items, {}),
            ),
            {"fallbackReason": type(exc).__name__ if catalog else "NO_SAFE_CANDIDATES",
             "detail": redact(str(exc))[:200]},
        )
