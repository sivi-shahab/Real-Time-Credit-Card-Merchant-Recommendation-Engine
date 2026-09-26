"""Continuous learning stage 1: a live Postgres export must train exactly like the files.

Rows are shaped like the Postgres tables, written by the exporter, and rebuilt; the
training frame must match the one built from the generator's own files.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from rec.generator.config import DatasetConfig
from rec.generator.generate import generate
from rec.ml.dataset import _merchant, _promotion, _ts, build
from rec.ml.live_dataset import write_dataset


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_live_export_builds_the_same_training_frame(tmp_path):
    src = tmp_path / "generated"
    generate(DatasetConfig(seed=11, idNamespace="livetest", customerCount=80, merchantCount=25,
                           promotionCount=6, transactionCount=1500, historyDays=90,
                           outputFormats=["jsonl"]), src)
    feedback = _lines(src / "feedback_events.jsonl")

    write_dataset(
        tmp_path / "live",
        dataset_id="live",
        customers=[{"customer_id": c["customerId"], "city_code": c["cityCode"],
                    "card_tier": c["cardTier"],
                    "personalization_allowed": c["personalizationAllowed"]}
                   for c in _lines(src / "customers.jsonl")],
        merchants=[_merchant(m) for m in _lines(src / "merchants.jsonl")],
        promotions=[_promotion(p) for p in _lines(src / "promotions.jsonl")],
        envelopes=(src / "replay.jsonl").read_text().splitlines(),
        impressions=[{"request_id": e["requestId"], "impression_id": e["impressionId"],
                      "customer_id": e["customerId"], "merchant_id": e["merchantId"],
                      "position": e["position"], "occurred_at": _ts(e["occurredAt"])}
                     for e in feedback if e["eventType"] == "IMPRESSION"],
        interactions=[{"impression_id": e["impressionId"], "customer_id": e["customerId"],
                       "merchant_id": e["merchantId"], "interaction_type": e["eventType"],
                       "occurred_at": _ts(e["occurredAt"])}
                      for e in feedback if e["eventType"] != "IMPRESSION"],
    )

    expected, _ = build(src)
    actual, meta = build(tmp_path / "live")
    assert len(expected) > 0
    assert meta["rows"] == len(expected)
    pd.testing.assert_frame_equal(actual, expected)
