"""ADR-0010: promo holdout assignment and the uplift estimator."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from rec.ml import uplift


def test_holdout_is_deterministic_proportional_and_independent_of_canary():
    from rec.ml.registry import in_canary

    ids = [f"C{n:07d}" for n in range(5000)]
    assert [uplift.in_holdout(c, 10) for c in ids] == [uplift.in_holdout(c, 10) for c in ids]
    assert not any(uplift.in_holdout(c, 0) for c in ids)
    share = sum(uplift.in_holdout(c, 10) for c in ids) / len(ids)
    assert 0.08 < share < 0.12
    both = sum(uplift.in_holdout(c, 50) and in_canary(c, 50) for c in ids) / len(ids)
    assert 0.22 < both < 0.28  # a different salt: the two splits are not the same cohort


def test_features_are_taken_as_of_first_exposure():
    exposed = datetime(2026, 9, 1, tzinfo=UTC)
    frame = uplift.features(pd.DataFrame([{
        "customer_id": "C1", "arm": "HOLDOUT", "holdout_percent": 10,
        "first_exposed_at": exposed, "card_tier": "GOLD", "txn_count_90d": 4,
        "spend_90d": 999, "last_txn": exposed - timedelta(days=3),
        "first_txn": exposed - timedelta(days=40), "txn_before": 8, "promo_txn_before": 2,
        "converted": True}]))
    row = frame.iloc[0]
    assert row["days_since_last_txn"] == 3 and row["history_days"] == 40
    assert row["promo_purchase_share"] == 0.25 and row["card_tier_rank"] == 1
    assert row["treatment"] == 0 and row["converted"] == 1
    assert row["propensity"] == pytest.approx(0.9)


def test_x_learner_finds_the_persuadables_better_than_random():
    pytest.importorskip("causalml")
    rng = np.random.default_rng(0)
    n = 4000
    frame = pd.DataFrame({name: rng.random(n) for name in uplift.FEATURES})
    frame["treatment"] = (rng.random(n) < 0.8).astype(int)
    frame["propensity"] = 0.8
    # only frequent promo buyers respond to an offer
    effect = np.where(frame["promo_purchase_share"] > 0.5, 0.3, 0.0)
    frame["converted"] = (rng.random(n) < 0.2 + effect * frame["treatment"]).astype(int)

    report = uplift.fit(frame)
    assert report["qini"]["model"] > report["qini"]["random"] + 0.1
    assert report["averageEffect"] == pytest.approx(0.15, abs=0.04)
    assert report["segments"]["persuadable"] > 0.2
