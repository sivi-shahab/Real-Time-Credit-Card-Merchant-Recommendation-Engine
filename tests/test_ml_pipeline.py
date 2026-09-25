"""SDD 16 ML level — temporal split, leakage, feature parity, reproducibility.

Uses a small generated dataset; no Postgres, Redis or Kafka.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from rec.core.models import Channel, Merchant
from rec.generator.config import DatasetConfig
from rec.generator.generate import generate
from rec.ml.dataset import GROUP_COLUMN, build, temporal_split
from rec.ml.train import baseline_score, train
from rec.ml.vectorize import FEATURE_NAMES, vectorize

SMALL = dict(customerCount=120, merchantCount=30, promotionCount=8, transactionCount=2500,
             historyDays=120, outputFormats=["jsonl"])


@pytest.fixture(scope="module")
def dataset_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("mlds") / "ds"
    generate(DatasetConfig(seed=5, idNamespace="mltest", **SMALL), out)
    return out


@pytest.fixture(scope="module")
def built(dataset_dir):
    return build(dataset_dir)


# ----------------------------------------------------------------- split


def test_temporal_split_respects_the_observation_gap(built):
    frame, _ = built
    split = temporal_split(frame, test_fraction=0.25)
    assert not split.train.empty and not split.test.empty
    assert split.train["requestTime"].max() <= split.boundary
    assert split.test["requestTime"].min() > split.boundary + timedelta(days=split.gap_days)
    assert not (set(split.train[GROUP_COLUMN]) & set(split.test[GROUP_COLUMN]))


def test_groups_are_intact_never_split_across_sides(built):
    frame, _ = built
    split = temporal_split(frame)
    for side in (split.train, split.test):
        # every request keeps all of its exposures, otherwise NDCG is computed on a
        # truncated slate and the metric silently lies
        sizes = side.groupby(GROUP_COLUMN).size()
        original = frame.groupby(GROUP_COLUMN).size()
        assert (sizes == original[sizes.index]).all()


# ----------------------------------------------------------------- leakage


def test_no_future_transaction_can_reach_a_past_training_row(dataset_dir, tmp_path):
    """Append an enormous transaction dated AFTER the last request and rebuild.

    Every feature row must be byte-identical: if a future event moved any number, the
    point-in-time join leaks.
    """
    before, _ = build(dataset_dir)
    latest_request = before["requestTime"].max()

    polluted = tmp_path / "polluted"
    polluted.mkdir()
    for name in ("customers", "merchants", "promotions", "feedback_events", "transactions"):
        (polluted / f"{name}.jsonl").write_text((dataset_dir / f"{name}.jsonl").read_text())
    replay = (dataset_dir / "replay.jsonl").read_text().splitlines()
    template = json.loads(replay[0])
    future = json.loads(json.dumps(template))
    future["eventId"] = "future-event-0001"
    future["occurredAt"] = (latest_request + timedelta(days=1)).isoformat()
    future["payload"] = template["payload"] | {
        "transactionId": "future-txn-0001",
        "amountMinor": 999_000_000,
        "occurredAt": (latest_request + timedelta(days=1)).isoformat(),
        "transactionType": "PURCHASE",
        "originalTransactionId": None,
    }
    (polluted / "replay.jsonl").write_text(
        "\n".join(replay + [json.dumps(future)]) + "\n")

    after, _ = build(polluted)
    columns = [GROUP_COLUMN, "merchantId", *FEATURE_NAMES]
    left = before[columns].sort_values([GROUP_COLUMN, "merchantId"]).reset_index(drop=True)
    right = after[columns].sort_values([GROUP_COLUMN, "merchantId"]).reset_index(drop=True)
    assert left.equals(right), "a future transaction changed a past feature row"


def test_labels_never_come_from_beyond_the_attribution_window(dataset_dir):
    frame, meta = build(dataset_dir, attribution_window=timedelta(seconds=0))
    # with a zero-width attribution window nothing can be credited
    assert set(frame["label"].unique()) == {0}
    assert meta["attribution"]["rule"] == "last-touch"


# ----------------------------------------------------------------- parity


def test_serving_and_training_produce_identical_vectors(built):
    """The serving wrapper must pass the same arguments the dataset builder passes.
    Swapping merchant_city and city_code, for instance, would only show up here."""
    from rec.api.service import _vectors

    frame, _ = built
    row = frame.iloc[0]
    features = {
        "windows": {"1": {"transactionCount": 1, "netSpendMinor": 10_000,
                          "averageSpendMinor": 10_000},
                    "7": {"transactionCount": 2, "netSpendMinor": 20_000,
                          "averageSpendMinor": 10_000},
                    "30": {"transactionCount": 4, "netSpendMinor": 40_000,
                           "averageSpendMinor": 10_000},
                    "90": {"transactionCount": 8, "netSpendMinor": 80_000,
                           "averageSpendMinor": 10_000}},
        "categoryInterest": {"F&B": 0.5}, "categoryFrequencyShare": {"F&B": 0.5},
        "categoryMonetaryShare": {"F&B": 0.5}, "categoryRecency": {"F&B": 0.7},
        "merchantAffinity": {"M1": {"count": 3, "recency": 0.6}},
        "activeHourDistribution": {9: 3}, "daysSinceLastTransaction": 1,
        "historyDepthDays": 90, "coldStartFlag": False,
        "featureAsOf": row["requestTime"].isoformat(),
    }
    merchant = Merchant(merchantId="M1", merchantName="M One", categoryCode="F&B",
                        cityCode="JKT", channel=Channel.OFFLINE, rating=4.2)
    request_time = row["requestTime"].to_pydatetime()
    promo = {"benefitValue": 10.0, "minSpendMinor": 100_000,
             "endsAt": (request_time + timedelta(days=2)).isoformat()}

    serving = _vectors([merchant], features, city_code="JKT",
                       eligible={"M1": promo}, now=request_time)[0]
    training = vectorize(features, merchant_id="M1", merchant_rating=4.2,
                        merchant_channel="OFFLINE", merchant_category="F&B",
                        merchant_city="JKT", city_code="JKT", promo=promo,
                        request_time=request_time)
    assert serving == training


def test_baseline_score_matches_the_production_formula(built):
    from rec.core.ranking import score_baseline

    frame, _ = built
    scores = baseline_score(frame)
    for i in range(min(25, len(frame))):
        row = frame.iloc[i]
        merchant = Merchant(merchantId=row["merchantId"], merchantName="x",
                            categoryCode=row["categoryCode"], cityCode="JKT",
                            channel=Channel.OFFLINE,
                            rating=float(row["merchant_rating"]))
        expected, _, _ = score_baseline(
            merchant,
            {"categoryInterest": {row["categoryCode"]: float(row["category_interest"])}},
            city_code="JKT" if row["merchant_is_local"] else "BDG",
            promo_eligible=bool(row["promo_eligible"]))
        assert abs(expected - float(scores.iloc[i])) < 1e-9


# ----------------------------------------------------------------- reproducibility


def test_training_is_reproducible_and_reports_its_gates(dataset_dir, tmp_path):
    first = train(dataset_dir, out_dir=tmp_path / "a", num_rounds=40,
                  mlflow_tracking_uri=None)
    second = train(dataset_dir, out_dir=tmp_path / "b", num_rounds=40,
                   mlflow_tracking_uri=None)
    assert first.metrics["ndcg@10"] == second.metrics["ndcg@10"]
    assert first.metrics["ndcg@5"] == second.metrics["ndcg@5"]
    assert first.datasetLineage["trainGroups"] == second.datasetLineage["trainGroups"]
    assert {g.name for g in first.gates} == {g.name for g in second.gates}
    assert first.modelVersion != second.modelVersion  # version is timestamped
    assert Path(first.artifacts["model"]).exists()
    assert first.metrics["inferenceLatencyMsP95"] < 500.0


def test_gates_block_a_model_that_loses_to_baseline_or_blows_the_budget(tmp_path):
    """The gate logic is tested directly: whether XGBoost happens to underperform on a
    particular synthetic dataset is not a reliable way to exercise a promotion gate."""
    from rec.ml.train import LATENCY_BUDGET_MS, _gates

    artifact = tmp_path / "m.json"
    artifact.write_text("{}")
    good = {"ndcg@10": 0.50, "ndcg@5": 0.48, "inferenceLatencyMsP95": 10.0}
    base = {"ndcg@10": 0.45, "ndcg@5": 0.44}

    passing = {g.name: g for g in _gates(good, base, {}, artifact)}
    assert all(g.passed for g in passing.values())

    worse = {g.name: g for g in _gates(
        good | {"ndcg@10": 0.40}, base, {}, artifact)}
    assert not worse["ndcg_not_worse_than_baseline"].passed

    slow = {g.name: g for g in _gates(
        good | {"inferenceLatencyMsP95": LATENCY_BUDGET_MS + 1}, base, {}, artifact)}
    assert not slow["latency_within_budget"].passed

    missing = {g.name: g for g in _gates(good, base, {}, tmp_path / "absent.json")}
    assert not missing["artifacts_complete"].passed

    regressed = {g.name: g for g in _gates(good, base, {
        "segment_history": {
            "model": {"COLD": {"ndcg@10": 0.30}},
            "baseline": {"COLD": {"ndcg@10": 0.45}},
        }}, artifact)}
    assert not regressed["no_critical_segment_regression"].passed
    assert "segment_history=COLD" in regressed["no_critical_segment_regression"].detail
