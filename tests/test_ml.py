"""Fase 4 — attribution, labels, leakage, feature parity, gates, canary determinism."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rec.ml.attribution import (
    Exposure,
    NotObservable,
    Outcome,
    attribute_conversion,
    label_exposure,
    label_from_events,
)
from rec.ml.registry import in_canary
from rec.ml.vectorize import FEATURE_NAMES, to_row, vectorize

T0 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)


def exposure(impression_id="i1", at=T0, position=0) -> Exposure:
    return Exposure("r1", impression_id, "C1", "M1", position, at)


# ----------------------------------------------------------------- ML-003 labels


def test_label_precedence_takes_the_highest_valid_outcome():
    outcomes = [
        Outcome("i1", "CLICK", T0 + timedelta(minutes=1)),
        Outcome("i1", "REDEMPTION", T0 + timedelta(hours=2)),
        Outcome("i1", "PROMO_ACTIVATION", T0 + timedelta(minutes=5)),
    ]
    assert label_exposure(exposure(), outcomes, as_of=T0 + timedelta(days=2)) == 3


def test_impression_without_interaction_is_zero_only_after_the_window_closes():
    with pytest.raises(NotObservable):
        label_exposure(exposure(), [], as_of=T0 + timedelta(hours=1))
    assert label_exposure(exposure(), [], as_of=T0 + timedelta(days=2)) == 0


def test_outcome_outside_the_attribution_window_earns_no_credit():
    late = [Outcome("i1", "REDEMPTION", T0 + timedelta(days=8))]
    assert label_exposure(exposure(), late, as_of=T0 + timedelta(days=30)) == 0
    before = [Outcome("i1", "CLICK", T0 - timedelta(minutes=1))]
    assert label_exposure(exposure(), before, as_of=T0 + timedelta(days=30)) == 0


def test_outcomes_for_another_impression_are_ignored():
    other = [Outcome("i2", "REDEMPTION", T0 + timedelta(hours=1))]
    assert label_exposure(exposure(), other, as_of=T0 + timedelta(days=2)) == 0


def test_label_from_events_withholds_unobservable_impressions():
    events = [
        {"eventType": "IMPRESSION", "impressionId": "old", "requestId": "r1",
         "customerId": "C1", "merchantId": "M1", "position": 0,
         "occurredAt": T0.isoformat()},
        {"eventType": "IMPRESSION", "impressionId": "fresh", "requestId": "r2",
         "customerId": "C1", "merchantId": "M2", "position": 0,
         "occurredAt": (T0 + timedelta(days=5)).isoformat()},
        {"eventType": "CLICK", "impressionId": "old", "requestId": "r1",
         "customerId": "C1", "merchantId": "M1",
         "occurredAt": (T0 + timedelta(minutes=2)).isoformat()},
    ]
    labels = label_from_events(events, as_of=T0 + timedelta(days=5, hours=1))
    assert labels == {"old": 1}, "a fresh impression must not be labelled a negative"


def test_conversion_gets_exactly_one_credited_exposure():
    conversion = Outcome("i1", "REDEMPTION", T0 + timedelta(days=1))
    credited = attribute_conversion(conversion, [exposure(), exposure(at=T0 - timedelta(days=30))])
    assert credited is not None and credited.shownAt == T0
    assert attribute_conversion(Outcome("zzz", "REDEMPTION", T0), [exposure()]) is None


# ----------------------------------------------------------------- vectoriser


def _features(**overrides) -> dict:
    base = {
        "windows": {"1": {"transactionCount": 1, "netSpendMinor": 50_000,
                          "averageSpendMinor": 50_000},
                    "7": {"transactionCount": 3, "netSpendMinor": 300_000,
                          "averageSpendMinor": 100_000},
                    "30": {"transactionCount": 9, "netSpendMinor": 900_000,
                           "averageSpendMinor": 100_000},
                    "90": {"transactionCount": 20, "netSpendMinor": 2_000_000,
                           "averageSpendMinor": 100_000}},
        "categoryInterest": {"F&B": 0.6}, "categoryFrequencyShare": {"F&B": 0.5},
        "categoryMonetaryShare": {"F&B": 0.4}, "categoryRecency": {"F&B": 0.9},
        "merchantAffinity": {"M1": {"count": 4, "recency": 0.8}},
        "activeHourDistribution": {10: 5, 12: 5},
        "daysSinceLastTransaction": 2, "historyDepthDays": 120, "coldStartFlag": False,
        "featureAsOf": T0.isoformat(),
    }
    return base | overrides


def _vec(features: dict, **overrides):
    kwargs = dict(merchant_id="M1", merchant_rating=4.5, merchant_channel="OFFLINE",
                  merchant_category="F&B", merchant_city="JKT", city_code="JKT",
                  promo=None, request_time=T0)
    return vectorize(features, **(kwargs | overrides))


def test_vector_has_every_declared_feature_and_no_extras():
    vector = _vec(_features())
    assert set(vector) == set(FEATURE_NAMES)
    assert len(to_row(vector)) == len(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in to_row(vector))


def test_position_is_never_a_feature():
    """Position is exposure bias and does not exist at ranking time."""
    assert not any("position" in name for name in FEATURE_NAMES)


def test_affinity_resolves_by_merchant_id_not_by_city():
    known = _vec(_features())
    unknown = _vec(_features(), merchant_id="M-OTHER")
    assert known["log_merchant_affinity_count"] > 0
    assert unknown["log_merchant_affinity_count"] == 0
    assert unknown["merchant_affinity_recency"] == 0.0


def test_missing_inputs_become_explicit_sentinels_not_silent_zeros():
    empty = _vec({})
    assert empty["days_since_last_txn"] == 999.0
    assert empty["feature_age_hours"] == 999.0
    assert empty["promo_days_remaining"] == 999.0
    assert empty["category_interest"] == 0.0


def test_promo_features_reflect_an_eligible_offer():
    promo = {"benefitValue": 15.0, "minSpendMinor": 250_000,
             "endsAt": (T0 + timedelta(days=3)).isoformat()}
    vector = _vec(_features(), promo=promo)
    assert vector["promo_eligible"] == 1.0
    assert vector["promo_has_min_spend"] == 1.0
    assert 2.9 < vector["promo_days_remaining"] < 3.1


def test_optout_customer_carries_the_flag_and_no_behaviour():
    vector = _vec(_features(categoryInterest={}, merchantAffinity={},
                            personalizationAllowed=False))
    assert vector["personalization_allowed"] == 0.0
    assert vector["category_interest"] == 0.0
    assert vector["log_merchant_affinity_count"] == 0.0


def test_locality_and_channel_are_encoded():
    assert _vec(_features(), merchant_city="BDG")["merchant_is_local"] == 0.0
    assert _vec(_features())["merchant_is_local"] == 1.0
    assert _vec(_features(), merchant_channel="ONLINE")["merchant_is_online"] == 1.0


# ----------------------------------------------------------------- canary


def test_canary_split_is_deterministic_and_roughly_proportional():
    ids = [f"C{i:06d}" for i in range(4000)]
    assert all(in_canary(i, 0) is False for i in ids[:50])
    assert all(in_canary(i, 100) is True for i in ids[:50])
    share = sum(in_canary(i, 25) for i in ids) / len(ids)
    assert 0.21 < share < 0.29, share
    assert all(in_canary(i, 25) == in_canary(i, 25) for i in ids[:100])


def test_guardrail_p95_is_a_conservative_bucket_bound():
    from rec.ml.guardrail import evaluate, p95_upper_ms

    assert p95_upper_ms({"lat_50": 95, "lat_1000": 5}) == 50
    assert p95_upper_ms({"lat_50": 90, "lat_1000": 10}) == 1000
    assert evaluate({"requests": 3, "degraded": 3}) == [], "too little data to judge"
