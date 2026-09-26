"""ML-002/005/006 — train an XGBoost ranker, evaluate it against the live baseline,
and apply the promotion gates. Offline scores alone never promote a model (ML-006).
"""
from __future__ import annotations

import json
import platform
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from rec.core.ranking import BASELINE_WEIGHTS
from rec.ml import metrics as M
from rec.ml.dataset import GROUP_COLUMN, LABEL_COLUMN, build, temporal_split
from rec.ml.vectorize import FEATURE_NAMES
from rec.ml.vectorize import FEATURE_SCHEMA_VERSION as VECTOR_SCHEMA_VERSION

TRAINER_VERSION = "1.0.0"
LATENCY_BUDGET_MS = 500.0  # NFR: on-demand ranking path p95
K = 10          # the spec's reporting cut (SDD ML-006)
SECONDARY_K = 5  # a real cut inside an 8-item slate, where @10 keeps everything
VALIDATION_FRACTION = 0.2  # of the training period: early stopping and tuning, never test

DEFAULT_PARAMS = {
    "objective": "rank:ndcg",
    "eval_metric": ["ndcg@10"],
    "lambdarank_pair_method": "topk",
    "lambdarank_num_pair_per_sample": 8,
    # Unbiased LambdaMART (ADR-0008): clicks are discounted by display position, which
    # the model estimates jointly. Norm 0.5 recovered the generator's known position curve
    # within MAE 0.03; the default 1.0 flattened it (MAE 0.11).
    "lambdarank_unbiased": True,
    "lambdarank_bias_norm": 0.5,
    "eta": 0.08,
    "max_depth": 6,
    "min_child_weight": 5,
    "subsample": 0.9,
    "colsample_bytree": 0.9,
    "reg_lambda": 1.0,
    "tree_method": "hist",
    "seed": 42,
}


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass
class TrainingResult:
    modelVersion: str
    trainerVersion: str = TRAINER_VERSION
    featureSchemaVersion: str = VECTOR_SCHEMA_VERSION
    params: dict = field(default_factory=dict)
    datasetLineage: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    baselineMetrics: dict = field(default_factory=dict)
    segmentMetrics: dict = field(default_factory=dict)
    gates: list[Gate] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    mlflowRunId: str | None = None

    @property
    def approved(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["approved"] = self.approved
        return data


def baseline_score(frame: pd.DataFrame) -> pd.Series:
    """ML-001 recomputed from the same feature columns the model sees, so the
    comparison is the live baseline and not an approximation of it."""
    w = BASELINE_WEIGHTS
    return (w["interest"] * frame["category_interest"].clip(0, 1)
            + w["location"] * frame["merchant_is_local"]
            + w["rating"] * (frame["merchant_rating"] / 5.0).clip(0, 1)
            + w["promo"] * frame["promo_eligible"])


def _dmatrix(frame: pd.DataFrame) -> xgb.DMatrix:
    """Rows must be contiguous per group for XGBoost's group sizes to mean anything, and
    in shown order inside a group: `lambdarank_unbiased` reads row order as position."""
    ordered = frame.sort_values([GROUP_COLUMN, "position"], kind="mergesort")
    sizes = ordered.groupby(GROUP_COLUMN, sort=True).size().to_numpy()
    matrix = xgb.DMatrix(ordered[list(FEATURE_NAMES)].to_numpy(dtype=np.float32),
                         label=ordered[LABEL_COLUMN].to_numpy(dtype=np.float32),
                         feature_names=list(FEATURE_NAMES))
    matrix.set_group(sizes)
    return matrix


# Where tuning looks. Keys the caller set explicitly are held fixed, not searched.
SEARCH_SPACE = {
    "eta": ("float", 0.02, 0.3, True),
    "max_depth": ("int", 3, 10, False),
    "min_child_weight": ("float", 1.0, 20.0, True),
    "subsample": ("float", 0.5, 1.0, False),
    "colsample_bytree": ("float", 0.5, 1.0, False),
    "reg_lambda": ("float", 0.1, 10.0, True),
}


def _tune(dfit: xgb.DMatrix, dval: xgb.DMatrix, validation: pd.DataFrame, base: dict,
          caller: dict, trials: int, num_rounds: int, early_stopping_rounds: int) -> dict:
    """Optuna TPE (seeded, so reruns agree) maximising NDCG@SECONDARY_K on validation."""
    import optuna  # only tuned jobs pay the import

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    x_val = xgb.DMatrix(validation[list(FEATURE_NAMES)].to_numpy(dtype=np.float32),
                        feature_names=list(FEATURE_NAMES))
    searched = {k: v for k, v in SEARCH_SPACE.items() if k not in caller}

    def objective(trial: optuna.Trial) -> float:
        suggested = {
            name: (trial.suggest_int(name, lo, hi) if kind == "int"
                   else trial.suggest_float(name, lo, hi, log=log))
            for name, (kind, lo, hi, log) in searched.items()}
        booster = xgb.train({**base, **suggested}, dfit, num_boost_round=num_rounds,
                            evals=[(dval, "validation")],
                            early_stopping_rounds=early_stopping_rounds, verbose_eval=False)
        scored = validation.assign(score=booster.predict(x_val))
        return M.grouped(scored, "score", k=SECONDARY_K)[f"ndcg@{SECONDARY_K}"]

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=base.get("seed", 42)))
    study.optimize(objective, n_trials=trials)
    return {"library": f"optuna {optuna.__version__}", "trials": trials,
            "metric": f"ndcg@{SECONDARY_K} on validation", "bestValue": study.best_value,
            "bestParams": study.best_params, "heldFixed": sorted(set(caller) & set(SEARCH_SPACE))}


def train(
    dataset_dir: Path,
    *,
    out_dir: Path,
    params: dict | None = None,
    num_rounds: int = 300,
    early_stopping_rounds: int = 30,
    test_fraction: float = 0.25,
    mlflow_tracking_uri: str | None = None,
    experiment: str = "merchant-ranking",
    exclude_customers: frozenset[str] = frozenset(),
    tune_trials: int = 0,
) -> TrainingResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    frame, dataset_meta = build(dataset_dir, exclude_customers=exclude_customers)
    if frame.empty:
        raise ValueError("no observable exposures in this dataset — nothing to train on")

    split = temporal_split(frame, test_fraction=test_fraction)
    if split.test.empty or split.train.empty:
        raise ValueError("temporal split produced an empty side; widen the dataset window")

    # The test side only grades the finished model (gates). Early stopping and tuning use
    # a later slice of the training period, so the gates are not graded on data that
    # already chose the number of trees or the hyperparameters.
    fit = temporal_split(split.train, test_fraction=VALIDATION_FRACTION)
    if fit.test.empty or fit.train.empty:
        raise ValueError("validation split produced an empty side; widen the dataset window")
    dfit, dval = _dmatrix(fit.train), _dmatrix(fit.test)

    fixed = {**DEFAULT_PARAMS, **(params or {})}
    tuning = _tune(dfit, dval, fit.test, fixed, params or {}, tune_trials, num_rounds,
                   early_stopping_rounds) if tune_trials else None
    merged = {**fixed, **(tuning["bestParams"] if tuning else {})}
    evals_result: dict = {}
    booster = xgb.train(
        merged, dfit, num_boost_round=num_rounds,
        evals=[(dfit, "train"), (dval, "validation")],
        early_stopping_rounds=early_stopping_rounds,
        evals_result=evals_result, verbose_eval=False,
    )

    test = split.test.copy()
    test["model_score"] = booster.predict(
        xgb.DMatrix(test[list(FEATURE_NAMES)].to_numpy(dtype=np.float32),
                    feature_names=list(FEATURE_NAMES)))
    test["baseline_score"] = baseline_score(test)

    model_metrics = M.grouped(test, "model_score", k=K)
    model_metrics |= M.grouped(test, "model_score", k=SECONDARY_K)
    model_metrics |= M.coverage_and_diversity(test, "model_score", k=SECONDARY_K)
    model_metrics["missingFeatureRate"] = M.missing_feature_rate(test, FEATURE_NAMES)
    model_metrics["inferenceLatencyMsP95"] = _latency_p95(booster, test)
    model_metrics["bestIteration"] = float(booster.best_iteration or booster.num_boosted_rounds())

    base_metrics = M.grouped(test, "baseline_score", k=K)
    base_metrics |= M.grouped(test, "baseline_score", k=SECONDARY_K)
    base_metrics |= M.coverage_and_diversity(test, "baseline_score", k=SECONDARY_K)

    segment_metrics = {
        segment: {
            "model": M.by_segment(test, "model_score", segment, k=K),
            "baseline": M.by_segment(test, "baseline_score", segment, k=K),
        }
        for segment in ("segment_history", "segment_city", "segment_card_tier")
    }

    model_version = f"xgb-rank-{datetime.now(UTC):%Y%m%d%H%M%S}"
    model_path = out_dir / f"{model_version}.json"
    booster.save_model(model_path)
    importance = booster.get_score(importance_type="gain")
    objective = json.loads(booster.save_config())["learner"]["objective"]

    result = TrainingResult(
        modelVersion=model_version,
        params=merged,
        datasetLineage={
            **dataset_meta,
            "trainRows": int(len(fit.train)), "validationRows": int(len(fit.test)),
            "testRows": int(len(split.test)),
            "trainGroups": int(fit.train[GROUP_COLUMN].nunique()),
            "testGroups": int(split.test[GROUP_COLUMN].nunique()),
            "splitBoundary": split.boundary.isoformat(), "gapDays": split.gap_days,
            "python": platform.python_version(), "xgboost": xgb.__version__,
            "slateSizeP50": float(split.test.groupby(GROUP_COLUMN).size().median()),
            "tuning": tuning,
            "metricCaveat": (
                f"Slates hold ~{int(split.test.groupby(GROUP_COLUMN).size().median())} items, so "
                f"@{K} keeps the whole slate: recall@{K} is trivially 1.0 and coverage/diversity "
                f"@{K} are identical across rankers. Read @{SECONDARY_K} for a real cut."
            ),
        },
        metrics=model_metrics,
        baselineMetrics=base_metrics,
        segmentMetrics=segment_metrics,
        gates=_gates(model_metrics, base_metrics, segment_metrics, model_path),
        artifacts={
            "model": str(model_path),
            "featureImportanceGain": dict(sorted(importance.items(), key=lambda kv: -kv[1])),
            "learningCurve": evals_result,
            # Estimated relative click propensity by display position (index 0 = top).
            "positionBias": {"clicked": objective["ti+"], "unclicked": objective["tj-"]}
            if "ti+" in objective else None,
        },
    )

    (out_dir / f"{model_version}.metadata.json").write_text(
        json.dumps(result.to_dict(), indent=2, default=str))
    result.mlflowRunId = _log_to_mlflow(result, booster, mlflow_tracking_uri, experiment)
    return result


def _latency_p95(booster: xgb.Booster, test: pd.DataFrame, *, batch: int = 200,
                 repeats: int = 30) -> float:
    """Batch inference latency for one recommendation request's candidate set."""
    sample = test[list(FEATURE_NAMES)].head(batch).to_numpy(dtype=np.float32)
    if not len(sample):
        return 0.0
    matrix = xgb.DMatrix(sample, feature_names=list(FEATURE_NAMES))
    timings = []
    for _ in range(repeats):
        started = time.perf_counter()
        booster.predict(matrix)
        timings.append((time.perf_counter() - started) * 1000)
    return float(np.percentile(timings, 95))


def _gates(model: dict, baseline: dict, segments: dict, model_path: Path) -> list[Gate]:
    """ML-006. A gate failing does not delete the model; it blocks promotion."""
    gates = [
        Gate("ndcg_not_worse_than_baseline",
             model[f"ndcg@{K}"] >= baseline[f"ndcg@{K}"],
             f"model {model[f'ndcg@{K}']:.4f} vs baseline {baseline[f'ndcg@{K}']:.4f}"),
        Gate(f"ndcg@{SECONDARY_K}_not_worse_than_baseline",
             model[f"ndcg@{SECONDARY_K}"] >= baseline[f"ndcg@{SECONDARY_K}"],
             f"model {model[f'ndcg@{SECONDARY_K}']:.4f} vs "
             f"baseline {baseline[f'ndcg@{SECONDARY_K}']:.4f}"),
        Gate("latency_within_budget",
             model["inferenceLatencyMsP95"] <= LATENCY_BUDGET_MS,
             f"p95 {model['inferenceLatencyMsP95']:.1f} ms <= {LATENCY_BUDGET_MS} ms"),
        Gate("feature_schema_compatible",
             VECTOR_SCHEMA_VERSION == "1.0.0",
             f"vectoriser schema {VECTOR_SCHEMA_VERSION}"),
        Gate("artifacts_complete", model_path.exists() and model_path.stat().st_size > 0,
             f"{model_path.name} present"),
    ]

    regressions: list[str] = []
    for segment, sides in segments.items():
        for value, model_side in sides["model"].items():
            base_side = sides["baseline"].get(value)
            if not base_side:
                continue
            delta = model_side[f"ndcg@{K}"] - base_side[f"ndcg@{K}"]
            if delta < -0.05:  # agreed critical-regression threshold
                regressions.append(f"{segment}={value} {delta:+.4f}")
    gates.append(Gate("no_critical_segment_regression", not regressions,
                      "; ".join(regressions) or "no segment regressed by more than 0.05 NDCG"))
    return gates


def _metric_key(prefix: str, name: str) -> str:
    return f"{prefix}_{name.replace('@', '_at_')}"


def _log_to_mlflow(result: TrainingResult, booster: xgb.Booster,
                   tracking_uri: str | None, experiment: str) -> str | None:
    """MLflow is the experiment log and model registry (SDD 3.2). It is optional:
    a missing tracking backend must not lose a trained model, whose artifact and
    metadata are already on disk before this runs."""
    if not tracking_uri:
        return None  # explicit opt-out, e.g. tests
    try:
        import mlflow

        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment)
    except Exception:
        return None
    with mlflow.start_run(run_name=result.modelVersion) as run:
        mlflow.log_params({k: str(v) for k, v in result.params.items()})
        mlflow.log_params({"featureSchemaVersion": result.featureSchemaVersion,
                           "trainerVersion": result.trainerVersion})
        # MLflow metric names reject "@", which our ranking metrics use.
        mlflow.log_metrics({_metric_key("model", k): float(v)
                            for k, v in result.metrics.items() if isinstance(v, (int, float))})
        mlflow.log_metrics({_metric_key("baseline", k): float(v)
                            for k, v in result.baselineMetrics.items()
                            if isinstance(v, (int, float))})
        mlflow.log_dict(result.datasetLineage, "dataset_lineage.json")
        mlflow.log_dict(result.segmentMetrics, "segment_metrics.json")
        mlflow.log_dict({g.name: {"passed": g.passed, "detail": g.detail}
                         for g in result.gates}, "gates.json")
        mlflow.log_dict(result.artifacts["featureImportanceGain"], "feature_importance.json")
        if result.datasetLineage.get("tuning"):
            mlflow.log_dict(result.datasetLineage["tuning"], "tuning.json")
        if result.artifacts.get("positionBias"):
            mlflow.log_dict(result.artifacts["positionBias"], "position_bias.json")
        mlflow.log_artifact(result.artifacts["model"])
        mlflow.set_tag("approved", str(result.approved))
        try:
            import mlflow.xgboost

            mlflow.xgboost.log_model(booster, name="model",
                                     registered_model_name=experiment)
        except Exception:
            pass  # registry unavailable; the run and the on-disk artifact still stand
        return run.info.run_id
