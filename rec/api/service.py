"""SERV-001..004 — recommendation orchestration, cache, fallback."""
from __future__ import annotations

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
from rec.settings import settings
from rec.store import pg
from rec.store.redis_store import OnlineStore


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


def cache_key(customer_id: str, city: str, channel: str | None, limit: int) -> str:
    """SERV-004 — context + model + ranking config are all part of the key."""
    return (f"{customer_id}|{city}|{channel or 'ANY'}|{limit}"
            f"|{settings.model_version}|{RANKING_CONFIG_VERSION}|{FEATURE_SCHEMA_VERSION}")


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
    key = cache_key(customer_id, city, channel, limit)

    if use_cache and not preview:
        with stage("cache"):
            cached = await store.get_cached(key)
        if cached:
            resp = RecommendationResponse.model_validate(cached)
            # AC-005: promos are re-validated on every serve, cache or not
            valid_ids = {p.merchantId for p in await pg.active_promotions(now)}
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
        if customer.personalizationAllowed:
            merchant_city = await pg.merchant_city_map()
            features = await store.features(customer_id, now, merchant_city)
        else:
            # CAND-003: no behavioural features when personalisation is refused
            features = {"categoryInterest": {}, "merchantAffinity": {}, "coldStartFlag": True,
                        "featureAsOf": now.isoformat(), "personalizationAllowed": False}

    with stage("catalog"):
        merchants = await pg.merchants()
        promos = await pg.active_promotions(now)
        redemptions = await pg.customer_redemption_counts(customer_id)

    by_merchant = {m.merchantId: m for m in merchants}
    with stage("promo"):
        eligible: dict[str, dict] = {}
        for promo in promos:
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

    with stage("ranking"):
        scored = []
        for merchant in candidates:
            score, parts, reasons = score_baseline(
                merchant, features, city_code=city,
                promo_eligible=merchant.merchantId in eligible,
            )
            scored.append((score, merchant, {"reasonCodes": reasons, "components": parts}))
        scored.sort(key=lambda x: (-x[0], x[1].merchantId))
        if explain:
            debug["scored"] = [
                {"merchantId": m.merchantId, "score": round(s, 6), **meta}
                for s, m, meta in scored[:50]
            ]
        top = diversify(scored, limit)

    response = RecommendationResponse(
        requestId=str(uuid.uuid4()),
        customerId=customer_id,
        generatedAt=now,
        featureAsOf=features.get("featureAsOf"),
        modelVersion=settings.model_version,
        source="LIVE",
        stale=bool(features.get("coldStartFlag")),
        recommendations=to_recommendations(top, eligible),
    )
    if not top:
        response.source = "FALLBACK"
        debug["fallbackReason"] = "NO_SAFE_CANDIDATES"

    if use_cache and not preview:
        with stage("cacheWrite"):
            await store.put_cached(key, response.model_dump(mode="json"))
    await store.incr_metric("recommendation_served")
    return response, debug


async def recommend_safe(store: OnlineStore, customer_id: str, **kwargs):
    """SERV-003 fallback chain. Never raises for a known customer."""
    try:
        return await recommend(store, customer_id, **kwargs)
    except KeyError:
        raise
    except Exception as exc:  # dependency failure -> popular, still promo-validated
        await store.incr_metric("recommendation_fallback")
        kwargs.pop("explain", None)
        now = kwargs.get("now") or datetime.now(UTC)
        merchants = sorted(
            [m for m in await pg.merchants() if m.status == "ACTIVE"],
            key=lambda m: (-m.rating, m.merchantId),
        )[: kwargs.get("limit", DEFAULT_RESULTS)]
        items = [(m.rating / 5.0, m, {"reasonCodes": ["POPULAR_PICK", "FALLBACK"]})
                 for m in merchants]
        return (
            RecommendationResponse(
                requestId=str(uuid.uuid4()), customerId=customer_id, generatedAt=now,
                featureAsOf=None, modelVersion=settings.model_version, source="FALLBACK",
                stale=True, recommendations=to_recommendations(items, {}),
            ),
            {"fallbackReason": type(exc).__name__, "detail": str(exc)[:200]},
        )
