"""CAND-001..003 + ML-001 baseline ranking. Pure scoring, no IO."""
from __future__ import annotations

from .models import Merchant, Recommendation

MAX_CANDIDATES = 200
MAX_RESULTS = 20
DEFAULT_RESULTS = 10

BASELINE_WEIGHTS = {"interest": 0.65, "location": 0.20, "rating": 0.10, "promo": 0.05}


def generate_candidates(
    merchants: list[Merchant],
    features: dict,
    *,
    city_code: str,
    channel: str | None = None,
    promo_merchant_ids: set[str] | None = None,
    exclusions: set[str] | None = None,
    blocked_categories: set[str] | None = None,
    limit: int = MAX_CANDIDATES,
) -> tuple[list[Merchant], list[dict]]:
    """Returns (candidates, dropped) — dropped carries a reason per UI-006."""
    exclusions = exclusions or set()
    blocked = blocked_categories or set()
    promo_ids = promo_merchant_ids or set()
    interest = features.get("categoryInterest") or {}
    affinity = features.get("merchantAffinity") or {}

    kept: list[Merchant] = []
    dropped: list[dict] = []
    for m in merchants:
        reason = None
        if m.status != "ACTIVE":
            reason = "MERCHANT_INACTIVE"
        elif m.merchantId in exclusions:
            reason = "EXCLUSION_LIST"
        elif m.categoryCode in blocked:
            reason = "CATEGORY_BLOCKED"
        elif channel and m.channel.value != channel:
            reason = "CHANNEL_MISMATCH"
        if reason:
            dropped.append({"merchantId": m.merchantId, "reason": reason})
            continue
        sourced = (
            m.categoryCode in interest
            or m.merchantId in affinity
            or m.cityCode == city_code
            or m.merchantId in promo_ids
        )
        if not sourced:
            dropped.append({"merchantId": m.merchantId, "reason": "NOT_IN_CANDIDATE_SOURCE"})
            continue
        kept.append(m)

    # prefer promo + local + affinity when trimming to the cap
    kept.sort(
        key=lambda m: (
            m.merchantId in promo_ids,
            m.merchantId in affinity,
            m.cityCode == city_code,
            m.rating,
        ),
        reverse=True,
    )
    if len(kept) > limit:
        for m in kept[limit:]:
            dropped.append({"merchantId": m.merchantId, "reason": "CANDIDATE_CAP"})
        kept = kept[:limit]
    return kept, dropped


def score_baseline(
    merchant: Merchant,
    features: dict,
    *,
    city_code: str,
    promo_eligible: bool,
    weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float], list[str]]:
    """ML-001. Relative relevance, NOT a click probability."""
    w = weights or BASELINE_WEIGHTS
    interest_map = features.get("categoryInterest") or {}
    interest = min(max(interest_map.get(merchant.categoryCode, 0.0), 0.0), 1.0)
    location = 1.0 if merchant.cityCode == city_code else 0.0
    rating = min(max(merchant.rating / 5.0, 0.0), 1.0)
    promo = 1.0 if promo_eligible else 0.0
    parts = {"interest": interest, "location": location, "rating": rating, "promo": promo}
    score = sum(w[k] * parts[k] for k in w)

    reasons: list[str] = []
    if interest >= 0.15:
        reasons.append("FAVORITE_CATEGORY")
    if merchant.merchantId in (features.get("merchantAffinity") or {}):
        reasons.append("FREQUENT_MERCHANT")
    if location:
        reasons.append("LOCAL_MERCHANT")
    if promo:
        reasons.append("CARD_PROMO_ELIGIBLE")
    if rating >= 0.9:
        reasons.append("TOP_RATED")
    if features.get("coldStartFlag"):
        reasons.append("POPULAR_PICK")
    return score, parts, reasons


def diversify(ranked: list[tuple[float, Merchant, dict]], limit: int, max_per_category: int = 3):
    """SERV-001 step 7 — cap per-category domination while preserving order."""
    seen: dict[str, int] = {}
    out, overflow = [], []
    for item in ranked:
        cat = item[1].categoryCode
        if seen.get(cat, 0) < max_per_category:
            seen[cat] = seen.get(cat, 0) + 1
            out.append(item)
        else:
            overflow.append(item)
        if len(out) >= limit:
            break
    return (out + overflow)[:limit]


def to_recommendations(items, promos_by_merchant: dict) -> list[Recommendation]:
    return [
        Recommendation(
            merchantId=m.merchantId,
            merchantName=m.merchantName,
            categoryCode=m.categoryCode,
            rank=i + 1,
            score=round(score, 6),
            reasonCodes=meta["reasonCodes"],
            promotion=promos_by_merchant.get(m.merchantId),
        )
        for i, (score, m, meta) in enumerate(items)
    ]
