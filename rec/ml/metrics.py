"""ML-006 evaluation metrics. Ranking metrics are computed per request group."""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd


def dcg(gains: Sequence[float], k: int) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))


def ndcg_at_k(labels: Sequence[int], scores: Sequence[float], k: int = 10) -> float:
    """Graded NDCG with 2^rel - 1 gain. A group with no relevant item scores 0."""
    order = np.argsort(-np.asarray(scores, dtype=float), kind="stable")
    ranked = [2 ** labels[i] - 1 for i in order]
    ideal = sorted((2 ** label - 1 for label in labels), reverse=True)
    best = dcg(ideal, k)
    return dcg(ranked, k) / best if best > 0 else 0.0


def recall_at_k(labels: Sequence[int], scores: Sequence[float], k: int = 10) -> float:
    """Share of this group's relevant items that survive into the top k."""
    relevant = sum(1 for label in labels if label > 0)
    if relevant == 0:
        return float("nan")
    order = np.argsort(-np.asarray(scores, dtype=float), kind="stable")[:k]
    return sum(1 for i in order if labels[i] > 0) / relevant


def grouped(frame: pd.DataFrame, score_column: str, *, k: int = 10) -> dict[str, float]:
    ndcgs, recalls = [], []
    for _, group in frame.groupby("requestId", sort=False):
        labels = group["label"].tolist()
        scores = group[score_column].tolist()
        ndcgs.append(ndcg_at_k(labels, scores, k))
        recalls.append(recall_at_k(labels, scores, k))
    recalls = [r for r in recalls if not math.isnan(r)]
    return {
        f"ndcg@{k}": float(np.mean(ndcgs)) if ndcgs else 0.0,
        f"recall@{k}": float(np.mean(recalls)) if recalls else 0.0,
        "groups": float(len(ndcgs)),
    }


def coverage_and_diversity(frame: pd.DataFrame, score_column: str, *, k: int = 10) -> dict:
    """Merchant coverage and category diversity of what the model would actually show."""
    shown_merchants: set[str] = set()
    diversities: list[float] = []
    for _, group in frame.groupby("requestId", sort=False):
        top = group.nlargest(k, score_column)
        shown_merchants.update(top["merchantId"])
        if len(top):
            diversities.append(top["categoryCode"].nunique() / len(top))
    catalogue = frame["merchantId"].nunique() or 1
    return {
        "merchantCoverage": len(shown_merchants) / catalogue,
        "merchantsShown": float(len(shown_merchants)),
        "categoryDiversity": float(np.mean(diversities)) if diversities else 0.0,
    }


def missing_feature_rate(frame: pd.DataFrame, feature_names: Sequence[str]) -> float:
    """Share of feature cells that carry a missing-value sentinel (999) or NaN."""
    block = frame[list(feature_names)]
    sentinel = (block == 999.0).to_numpy().sum()
    nan = block.isna().to_numpy().sum()
    return float((sentinel + nan) / max(block.size, 1))


def by_segment(frame: pd.DataFrame, score_column: str, segment: str, *,
               k: int = 10, min_groups: int = 30) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for value, block in frame.groupby(segment, sort=True):
        if block["requestId"].nunique() < min_groups:
            continue  # too few groups to read anything into
        out[str(value)] = grouped(block, score_column, k=k)
    return out
