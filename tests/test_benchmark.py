"""ADR-0009: offline challengers see train()'s splits and are graded the same way."""
from __future__ import annotations

import pytest

from rec.generator.config import DatasetConfig
from rec.generator.generate import generate
from rec.ml import benchmark


@pytest.fixture(scope="module")
def dataset_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("bench") / "ds"
    generate(DatasetConfig(seed=5, idNamespace="bench", customerCount=120, merchantCount=30,
                           promotionCount=8, transactionCount=2500, historyDays=120,
                           outputFormats=["jsonl"]), out)
    return out


def test_splits_are_ordered_in_time_and_disjoint(dataset_dir):
    fit, validation, test = benchmark.splits(dataset_dir)
    assert fit["requestTime"].max() < validation["requestTime"].min()
    assert validation["requestTime"].max() < test["requestTime"].min()


def test_catboost_challenger_is_graded_against_the_baseline(dataset_dir):
    pytest.importorskip("catboost")
    result = benchmark.run(dataset_dir, ["catboost"])["catboost"]
    assert set(result) == {"ndcg@5", "ndcg@10", "baseline_ndcg@5", "baseline_ndcg@10"}
    assert all(0.0 < v <= 1.0 for v in result.values())
