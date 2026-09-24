"""FEAT-001/002/004 — point-in-time feature computation. Pure, no IO."""
from __future__ import annotations

import math
from datetime import UTC, datetime

from .ledger import MAX_WINDOW_DAYS, CustomerState, in_window
from .models import FEATURE_SCHEMA_VERSION

WINDOWS = (1, 7, 30, 90)
INTEREST_WEIGHTS = {"frequency": 0.4, "monetary": 0.3, "recency": 0.3}
RECENCY_HALFLIFE_DAYS = 30.0
COLD_START_MIN_TXN = 3


def _normalised(weights: dict[str, float]) -> dict[str, float]:
    if any(v < 0 for v in weights.values()):
        raise ValueError("interest weights must be non-negative (FEAT-002)")
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("interest weights must sum to > 0")
    return {k: v / total for k, v in weights.items()}


def compute_features(
    state: CustomerState,
    as_of: datetime,
    *,
    weights: dict[str, float] | None = None,
    merchant_city: dict[str, str] | None = None,
) -> dict:
    """Snapshot per FEAT-004. `as_of` is the feature as-of time, never wall clock."""
    as_of = as_of.astimezone(UTC)
    w = _normalised(weights or INTEREST_WEIGHTS)
    merchant_city = merchant_city or {}

    windowed: dict[int, dict] = {}
    for days in WINDOWS:
        count = net = 0
        for _key, (c, n) in in_window(state, as_of, days):
            count += c
            net += n
        windowed[days] = {
            "transactionCount": count,
            "netSpendMinor": max(net, 0),
            "averageSpendMinor": (max(net, 0) // count) if count else 0,
        }

    cat_count: dict[str, int] = {}
    cat_net: dict[str, int] = {}
    cat_last: dict[str, str] = {}
    merch_count: dict[str, int] = {}
    merch_last: dict[str, str] = {}
    city_count: dict[str, int] = {}
    last_day = None
    for (day, cat, merch), (c, n) in in_window(state, as_of, MAX_WINDOW_DAYS):
        cat_count[cat] = cat_count.get(cat, 0) + c
        cat_net[cat] = cat_net.get(cat, 0) + max(n, 0)
        merch_count[merch] = merch_count.get(merch, 0) + c
        if c > 0:
            cat_last[cat] = max(cat_last.get(cat, ""), day)
            merch_last[merch] = max(merch_last.get(merch, ""), day)
            city = merchant_city.get(merch)
            if city:
                city_count[city] = city_count.get(city, 0) + c
            last_day = max(last_day or day, day)

    total_count = sum(cat_count.values())
    total_net = sum(cat_net.values())

    def _recency(day: str | None) -> float:
        if not day:
            return 0.0  # no history in category -> recency 0 (FEAT-002)
        delta = (as_of.date() - datetime.fromisoformat(day).date()).days
        return math.exp(-max(delta, 0) / RECENCY_HALFLIFE_DAYS)

    interest, freq_share, mon_share, recency = {}, {}, {}, {}
    for cat in cat_count:
        f = cat_count[cat] / max(total_count, 1)
        m = cat_net[cat] / max(total_net, 1)
        r = _recency(cat_last.get(cat))
        freq_share[cat], mon_share[cat], recency[cat] = f, m, r
        interest[cat] = w["frequency"] * f + w["monetary"] * m + w["recency"] * r

    affinity = {
        m: {"count": c, "recency": _recency(merch_last.get(m))}
        for m, c in merch_count.items()
        if c > 0
    }
    days_since = (as_of.date() - datetime.fromisoformat(last_day).date()).days if last_day else None
    first_day = min((k[0] for k in state.buckets), default=None)
    depth = (as_of.date() - datetime.fromisoformat(first_day).date()).days if first_day else 0

    return {
        "customerId": state.customerId,
        "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
        "featureVersion": state.featureVersion,
        "computedAt": datetime.now(UTC).isoformat(),
        "featureAsOf": as_of.isoformat(),
        "lastEventOccurredAt": state.lastEventOccurredAt.isoformat()
        if state.lastEventOccurredAt
        else None,
        "windows": {str(d): windowed[d] for d in WINDOWS},
        "transactionCount90d": windowed[90]["transactionCount"],
        "netSpend90d": windowed[90]["netSpendMinor"],
        "averageSpend90d": windowed[90]["averageSpendMinor"],
        "categoryInterest": interest,
        "categoryFrequencyShare": freq_share,
        "categoryMonetaryShare": mon_share,
        "categoryRecency": recency,
        "merchantAffinity": affinity,
        "preferredCity": max(city_count, key=city_count.get) if city_count else None,
        "activeHourDistribution": dict(sorted(state.hours.items())),
        "daysSinceLastTransaction": days_since,
        "historyDepthDays": depth,
        "coldStartFlag": windowed[90]["transactionCount"] < COLD_START_MIN_TXN,
    }


EMPTY_FEATURES_KEYS = ("categoryInterest", "merchantAffinity")


def empty_features(customer_id: str, as_of: datetime) -> dict:
    return compute_features(CustomerState(customerId=customer_id), as_of)
