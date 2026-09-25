"""ML-002 feature vectorisation — the single source of truth for feature parity.

Training and the ranking service both call `vectorize`. If they ever diverge the
model scores garbage at serving time, so there is deliberately no second copy.

Position is NOT a feature: it is exposure bias, and at ranking time the position
does not exist yet. It belongs to label construction only (ML-003).
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

FEATURE_SCHEMA_VERSION = "1.0.0"

FEATURE_NAMES: tuple[str, ...] = (
    # customer behaviour
    "txn_count_7d", "txn_count_30d", "txn_count_90d",
    "log_net_spend_30d", "log_net_spend_90d", "log_avg_spend_90d",
    "days_since_last_txn", "history_depth_days",
    # merchant
    "merchant_rating", "merchant_is_online",
    # customer x merchant
    "category_interest", "category_freq_share", "category_monetary_share",
    "category_recency", "log_merchant_affinity_count", "merchant_affinity_recency",
    "merchant_is_local",
    # promo
    "promo_eligible", "promo_benefit_value", "log_promo_min_spend",
    "promo_has_min_spend", "promo_days_remaining",
    # context
    "hour_sin", "hour_cos", "is_weekend", "customer_hour_activity_share",
    # feature availability metadata
    "cold_start", "personalization_allowed", "feature_age_hours",
)

_NO_RECENCY = 0.0
_MISSING_DAYS = 999.0  # sentinel: XGBoost splits on it, no imputation guesswork


def _log1p(value: float | int | None) -> float:
    return math.log1p(max(float(value or 0), 0.0))


def vectorize(
    features: dict,
    *,
    merchant_id: str,
    merchant_rating: float,
    merchant_channel: str,
    merchant_category: str,
    merchant_city: str,
    city_code: str,
    promo: dict | None,
    request_time: datetime,
) -> dict[str, float]:
    """One candidate -> one feature row. Missing inputs map to explicit sentinels."""
    request_time = request_time.astimezone(UTC)
    windows = features.get("windows") or {}

    def win(days: str, field: str) -> float:
        return float((windows.get(days) or {}).get(field, 0) or 0)

    interest = features.get("categoryInterest") or {}
    freq_share = features.get("categoryFrequencyShare") or {}
    mon_share = features.get("categoryMonetaryShare") or {}
    recency = features.get("categoryRecency") or {}
    affinity_entry = (features.get("merchantAffinity") or {}).get(merchant_id) or {}

    days_since = features.get("daysSinceLastTransaction")
    hours = features.get("activeHourDistribution") or {}
    hour_total = sum(hours.values()) or 1
    hour_key = request_time.hour

    feature_as_of = features.get("featureAsOf")
    if isinstance(feature_as_of, str):
        try:
            feature_as_of = datetime.fromisoformat(feature_as_of)
        except ValueError:
            feature_as_of = None
    age_hours = (
        max((request_time - feature_as_of.astimezone(UTC)).total_seconds(), 0) / 3600
        if isinstance(feature_as_of, datetime)
        else _MISSING_DAYS
    )

    promo_days_remaining = _MISSING_DAYS
    if promo and promo.get("endsAt"):
        ends = promo["endsAt"]
        if isinstance(ends, str):
            ends = datetime.fromisoformat(ends.replace("Z", "+00:00"))
        promo_days_remaining = max(
            (ends.astimezone(UTC) - request_time).total_seconds() / 86400, 0.0)

    return {
        "txn_count_7d": win("7", "transactionCount"),
        "txn_count_30d": win("30", "transactionCount"),
        "txn_count_90d": win("90", "transactionCount"),
        "log_net_spend_30d": _log1p(win("30", "netSpendMinor")),
        "log_net_spend_90d": _log1p(win("90", "netSpendMinor")),
        "log_avg_spend_90d": _log1p(win("90", "averageSpendMinor")),
        "days_since_last_txn": float(days_since) if days_since is not None else _MISSING_DAYS,
        "history_depth_days": float(features.get("historyDepthDays") or 0),
        "merchant_rating": float(merchant_rating),
        "merchant_is_online": 1.0 if merchant_channel == "ONLINE" else 0.0,
        "category_interest": float(interest.get(merchant_category, 0.0)),
        "category_freq_share": float(freq_share.get(merchant_category, 0.0)),
        "category_monetary_share": float(mon_share.get(merchant_category, 0.0)),
        "category_recency": float(recency.get(merchant_category, _NO_RECENCY)),
        "log_merchant_affinity_count": _log1p(affinity_entry.get("count", 0)),
        "merchant_affinity_recency": float(affinity_entry.get("recency", _NO_RECENCY)),
        "merchant_is_local": 1.0 if merchant_city == city_code else 0.0,
        "promo_eligible": 1.0 if promo else 0.0,
        "promo_benefit_value": float((promo or {}).get("benefitValue", 0.0)),
        "log_promo_min_spend": _log1p((promo or {}).get("minSpendMinor", 0)),
        "promo_has_min_spend": 1.0 if (promo or {}).get("minSpendMinor") else 0.0,
        "promo_days_remaining": promo_days_remaining,
        "hour_sin": math.sin(2 * math.pi * hour_key / 24),
        "hour_cos": math.cos(2 * math.pi * hour_key / 24),
        "is_weekend": 1.0 if request_time.weekday() >= 5 else 0.0,
        "customer_hour_activity_share": float(hours.get(hour_key, hours.get(str(hour_key), 0)))
        / hour_total,
        "cold_start": 1.0 if features.get("coldStartFlag") else 0.0,
        "personalization_allowed":
            0.0 if features.get("personalizationAllowed") is False else 1.0,
        "feature_age_hours": age_hours,
    }


def to_row(vector: dict[str, float]) -> list[float]:
    """Fixed column order — XGBoost is positional."""
    return [vector[name] for name in FEATURE_NAMES]
