"""Offline challengers for the ranking model (ADR-0009). Never on the serving path.

Each challenger gets the splits `train()` uses (fit, validation for early stopping, test
for grading) and is graded with the same metrics against the same baseline, so its row
sits beside the production trainer's without translation.

CatBoost runs in the project env: `uv pip install -e '.[benchmark]'`. LightAutoML pins
xgboost 2.1.x and pulls torch + CUDA, so it gets an env of its own and never the app image:

    uv venv .venv-automl
    uv pip install --python .venv-automl/bin/python --torch-backend cpu -e . lightautoml
    uv pip install --python .venv-automl/bin/python --torch-backend cpu "pandas<3"
    .venv-automl/bin/python -m rec.ml.benchmark <dataset_dir> \
        --challengers lightautoml,lightautoml-cat

    python -m rec.ml.benchmark <dataset_dir> [--challengers xgboost,xgboost-biased,catboost]
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from rec.ml import metrics as M
from rec.ml.dataset import GROUP_COLUMN, LABEL_COLUMN, build, temporal_split
from rec.ml.train import SECONDARY_K, VALIDATION_FRACTION, K, baseline_score, train
from rec.ml.vectorize import FEATURE_NAMES

# Raw ids the production model deliberately does not see (cold start, ADR-0005). The
# `-cat` variants add them to test the "native categorical" argument on our data.
CATEGORICAL = ["merchantId", "categoryCode"]


def splits(dataset_dir: Path, test_fraction: float = 0.25):
    frame, _ = build(dataset_dir)
    split = temporal_split(frame, test_fraction=test_fraction)
    fit = temporal_split(split.train, test_fraction=VALIDATION_FRACTION)
    return fit.train, fit.test, split.test


def grade(test: pd.DataFrame, scores) -> dict[str, float]:
    scored = test.assign(score=np.asarray(scores, dtype=float), baseline=baseline_score(test))
    out = {}
    for k in (SECONDARY_K, K):
        out[f"ndcg@{k}"] = M.grouped(scored, "score", k=k)[f"ndcg@{k}"]
        out[f"baseline_ndcg@{k}"] = M.grouped(scored, "baseline", k=k)[f"ndcg@{k}"]
    return out


def catboost(fit, validation, test, *, categorical: bool = False) -> np.ndarray:
    from catboost import CatBoostRanker, Pool

    cols = list(FEATURE_NAMES) + (CATEGORICAL if categorical else [])
    cats = CATEGORICAL if categorical else None

    def pool(frame: pd.DataFrame) -> Pool:
        frame = frame.sort_values([GROUP_COLUMN, "position"], kind="mergesort")
        return Pool(frame[cols], frame[LABEL_COLUMN], group_id=frame[GROUP_COLUMN].tolist(),
                    cat_features=cats)

    model = CatBoostRanker(loss_function="YetiRank", iterations=1000, learning_rate=0.08,
                           random_seed=42, od_type="Iter", od_wait=30, verbose=False,
                           allow_writing_files=False)
    model.fit(pool(fit), eval_set=pool(validation), use_best_model=True)
    return model.predict(Pool(test[cols], cat_features=cats))


def lightautoml(fit, validation, test, *, categorical: bool = False,
                timeout: int = 300) -> np.ndarray:
    """Pointwise: LightAutoML has no learning-to-rank task, so it predicts P(label > 0).
    Its internal CV is not temporal, which only mixes periods inside training."""
    from lightautoml.automl.presets.tabular_presets import TabularAutoML
    from lightautoml.tasks import Task

    cats = CATEGORICAL if categorical else []
    cols = list(FEATURE_NAMES) + cats
    data = pd.concat([fit, validation])
    data = data[cols].assign(target=(data[LABEL_COLUMN] > 0).astype(int))
    automl = TabularAutoML(task=Task("binary"), timeout=timeout,
                           reader_params={"random_state": 42})
    automl.fit_predict(data, roles={"target": "target", "category": cats})
    return automl.predict(test[cols]).data[:, 0]


def run(dataset_dir: Path, challengers: list[str], *, automl_timeout: int = 300) -> dict:
    fit, validation, test = splits(dataset_dir)
    results = {}
    for name in challengers:
        if name in ("xgboost", "xgboost-biased"):  # the production trainer, for reference
            # Test labels are raw clicks, which favour a ranker that was not debiased:
            # compare CatBoost (no position debiasing) with xgboost-biased.
            params = {"lambdarank_unbiased": False} if name.endswith("-biased") else None
            with tempfile.TemporaryDirectory() as tmp:
                m = train(dataset_dir, out_dir=Path(tmp), params=params).metrics
            base = grade(test, baseline_score(test))
            results[name] = {**{k: m[k] for k in (f"ndcg@{SECONDARY_K}", f"ndcg@{K}")},
                             **{k: v for k, v in base.items() if k.startswith("baseline")}}
        elif name in ("catboost", "catboost-cat"):
            results[name] = grade(test, catboost(fit, validation, test,
                                                 categorical=name.endswith("-cat")))
        elif name in ("lightautoml", "lightautoml-cat"):
            results[name] = grade(test, lightautoml(fit, validation, test,
                                                    categorical=name.endswith("-cat"),
                                                    timeout=automl_timeout))
        else:
            raise ValueError(f"unknown challenger {name}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dataset_dir", type=Path)
    parser.add_argument("--challengers",
                        default="xgboost,xgboost-biased,catboost,catboost-cat")
    parser.add_argument("--automl-timeout", type=int, default=300)
    args = parser.parse_args()
    results = run(args.dataset_dir, args.challengers.split(","),
                  automl_timeout=args.automl_timeout)
    print(f"{'challenger':14} {'ndcg@5':>8} {'ndcg@10':>8}   baseline @5 / @10")
    for name, r in results.items():
        print(f"{name:14} {r['ndcg@5']:8.4f} {r['ndcg@10']:8.4f}   "
              f"{r['baseline_ndcg@5']:.4f} / {r['baseline_ndcg@10']:.4f}")


if __name__ == "__main__":
    main()
