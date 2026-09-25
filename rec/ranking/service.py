"""Ranking Service (SDD 3.2) — batch inference over one request's candidate set.

A separate process on purpose: it makes the serving path's timeout and fallback real
rather than simulated, and lets a model be loaded or swapped without restarting the API.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

import numpy as np
import xgboost as xgb
from fastapi import FastAPI, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from rec.ml.vectorize import FEATURE_NAMES
from rec.ml.vectorize import FEATURE_SCHEMA_VERSION as VECTOR_SCHEMA_VERSION
from rec.obs import INFERENCE_LATENCY, setup_logging
from rec.settings import settings

app = FastAPI(title="Ranking Service", version="1.0.0", openapi_version="3.1.0",
              on_startup=[setup_logging])


# A model version becomes a file name: no separators, so no path traversal.
MODEL_VERSION = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class _Registry:
    """Loaded boosters, keyed by model version. Small and bounded in practice."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._boosters: dict[str, xgb.Booster] = {}

    def get(self, model_version: str) -> xgb.Booster:
        if not re.fullmatch(MODEL_VERSION, model_version):
            raise FileNotFoundError(f"invalid model version {model_version!r}")
        with self._lock:
            booster = self._boosters.get(model_version)
            if booster is not None:
                return booster
            path = Path(settings.model_dir) / f"{model_version}.json"
            if not path.exists():
                raise FileNotFoundError(str(path))
            booster = xgb.Booster()
            booster.load_model(path)
            # Serving scores <=200 rows per request (CAND-001). With the default thread
            # count XGBoost spends ~120 ms spawning and joining a 16-thread pool per
            # call — constant, independent of row count, and most of the latency budget.
            booster.set_param({"nthread": settings.ranking_threads})
            self._boosters[model_version] = booster
            return booster

    def loaded(self) -> list[str]:
        with self._lock:
            return sorted(self._boosters)

    def evict(self, model_version: str) -> bool:
        with self._lock:
            return self._boosters.pop(model_version, None) is not None


registry = _Registry()


class ScoreRequest(BaseModel):
    modelVersion: str
    featureSchemaVersion: str = VECTOR_SCHEMA_VERSION
    candidates: list[dict[str, float]] = Field(min_length=1, max_length=500)


class ScoreResponse(BaseModel):
    modelVersion: str
    featureSchemaVersion: str
    scores: list[float]
    inferenceMs: float
    scoredAt: datetime


@app.post("/v1/score", response_model=ScoreResponse, tags=["ranking"])
def score(body: ScoreRequest) -> ScoreResponse:
    """Batch inference. Rejects a feature-schema mismatch instead of scoring garbage."""
    if body.featureSchemaVersion != VECTOR_SCHEMA_VERSION:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"feature schema mismatch: caller {body.featureSchemaVersion}, "
            f"service {VECTOR_SCHEMA_VERSION}")
    try:
        booster = registry.get(body.modelVersion)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"model artifact missing: {exc}")

    missing = [name for name in FEATURE_NAMES if name not in body.candidates[0]]
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"missing features: {missing[:5]}")

    rows = np.asarray([[c[name] for name in FEATURE_NAMES] for c in body.candidates],
                      dtype=np.float32)
    started = time.perf_counter()
    scores = booster.predict(xgb.DMatrix(rows, feature_names=list(FEATURE_NAMES)))
    elapsed = (time.perf_counter() - started) * 1000
    INFERENCE_LATENCY.labels(body.modelVersion).observe(elapsed / 1000)
    return ScoreResponse(
        modelVersion=body.modelVersion, featureSchemaVersion=VECTOR_SCHEMA_VERSION,
        scores=[float(s) for s in scores], inferenceMs=round(elapsed, 3),
        scoredAt=datetime.now(UTC))


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {
        "status": "UP",
        "featureSchemaVersion": VECTOR_SCHEMA_VERSION,
        "featureCount": len(FEATURE_NAMES),
        "loadedModels": registry.loaded(),
        "modelDir": settings.model_dir,
        "nthread": settings.ranking_threads,
    }


@app.post("/v1/models/{model_version}/warm", tags=["ops"])
def warm(model_version: str) -> dict:
    """Pre-load before a canary so the first real request is not the cold one."""
    try:
        registry.get(model_version)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return {"modelVersion": model_version, "loaded": True}


@app.delete("/v1/models/{model_version}", tags=["ops"])
def evict(model_version: str) -> dict:
    return {"modelVersion": model_version, "evicted": registry.evict(model_version)}
