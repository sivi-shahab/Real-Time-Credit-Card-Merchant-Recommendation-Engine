"""Ranking Service (SDD 3.2) — batch inference over one request's candidate set.

A separate process on purpose: it makes the serving path's timeout and fallback real
rather than simulated, and lets a model be loaded or swapped without restarting the API.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

import numpy as np
import xgboost as xgb
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from rec.ml.vectorize import FEATURE_NAMES
from rec.ml.vectorize import FEATURE_SCHEMA_VERSION as VECTOR_SCHEMA_VERSION
from rec.obs import INFERENCE_LATENCY, setup_logging
from rec.settings import DEV_ENVIRONMENTS, settings

log = logging.getLogger("ranking")

app = FastAPI(title="Ranking Service", version="1.0.0", openapi_version="3.1.0",
              on_startup=[setup_logging])


# A model version becomes a file name: no separators, so no path traversal.
MODEL_VERSION = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
SHA256 = r"^[0-9a-f]{64}$"


class ArtifactIntegrityError(Exception):
    """The file on the volume is not the one training recorded (threat T-4)."""


class _Registry:
    """Loaded boosters, keyed by model version, least recently used evicted beyond
    `ranking_max_models` (D-5).

    Every load is checked against the SHA-256 the registry recorded at training (T-4):
    the digest comes from Postgres through the caller, never from the shared volume that
    holds the file, so replacing the file there cannot also replace what it is checked
    against.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._boosters: OrderedDict[str, tuple[xgb.Booster, str]] = OrderedDict()

    def get(self, model_version: str, sha256: str) -> xgb.Booster:
        if not re.fullmatch(MODEL_VERSION, model_version):
            raise FileNotFoundError(f"invalid model version {model_version!r}")
        with self._lock:
            loaded = self._boosters.get(model_version)
            if loaded is not None:
                booster, digest = loaded
                if not hmac.compare_digest(digest, sha256):
                    raise ArtifactIntegrityError(
                        f"{model_version}: loaded artifact does not match the recorded digest")
                self._boosters.move_to_end(model_version)
                return booster
            path = Path(settings.model_dir) / f"{model_version}.json"
            if not path.exists():
                raise FileNotFoundError(str(path))
            # Hash and parse the same bytes: reading the file twice would let it change
            # between the check and the load.
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if not hmac.compare_digest(digest, sha256):
                log.error("model %s artifact digest %s does not match recorded %s",
                          model_version, digest, sha256)
                raise ArtifactIntegrityError(
                    f"{model_version}: artifact does not match the digest recorded at training")
            booster = xgb.Booster()
            booster.load_model(bytearray(raw))
            # Serving scores <=200 rows per request (CAND-001). With the default thread
            # count XGBoost spends ~120 ms spawning and joining a 16-thread pool per
            # call — constant, independent of row count, and most of the latency budget.
            booster.set_param({"nthread": settings.ranking_threads})
            self._boosters[model_version] = (booster, digest)
            while len(self._boosters) > max(1, settings.ranking_max_models):
                self._boosters.popitem(last=False)
            return booster

    def loaded(self) -> list[str]:
        with self._lock:
            return sorted(self._boosters)

    def evict(self, model_version: str) -> bool:
        with self._lock:
            return self._boosters.pop(model_version, None) is not None


registry = _Registry()


def _caller(authorization: str = Header("")) -> None:
    """D-5: only the API may score, warm or evict. Network policy should say the same."""
    token = settings.ranking_service_token
    if not token:
        if settings.environment in DEV_ENVIRONMENTS:
            return
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "RANKING_SERVICE_TOKEN is not configured")
    if not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid service token")


class ScoreRequest(BaseModel):
    modelVersion: str
    modelSha256: str = Field(pattern=SHA256)
    featureSchemaVersion: str = VECTOR_SCHEMA_VERSION
    candidates: list[dict[str, float]] = Field(min_length=1, max_length=500)


class ScoreResponse(BaseModel):
    modelVersion: str
    featureSchemaVersion: str
    scores: list[float]
    inferenceMs: float
    scoredAt: datetime


@app.post("/v1/score", response_model=ScoreResponse, tags=["ranking"],
          dependencies=[Depends(_caller)])
def score(body: ScoreRequest) -> ScoreResponse:
    """Batch inference. Rejects a feature-schema mismatch instead of scoring garbage."""
    if body.featureSchemaVersion != VECTOR_SCHEMA_VERSION:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"feature schema mismatch: caller {body.featureSchemaVersion}, "
            f"service {VECTOR_SCHEMA_VERSION}")
    try:
        booster = registry.get(body.modelVersion, body.modelSha256)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"model artifact missing: {exc}")
    except ArtifactIntegrityError as exc:
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, str(exc))

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


class WarmRequest(BaseModel):
    modelSha256: str = Field(pattern=SHA256)


@app.post("/v1/models/{model_version}/warm", tags=["ops"], dependencies=[Depends(_caller)])
def warm(model_version: str, body: WarmRequest) -> dict:
    """Pre-load before a canary so the first real request is not the cold one."""
    try:
        registry.get(model_version, body.modelSha256)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except ArtifactIntegrityError as exc:
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, str(exc))
    return {"modelVersion": model_version, "loaded": True}


@app.delete("/v1/models/{model_version}", tags=["ops"], dependencies=[Depends(_caller)])
def evict(model_version: str) -> dict:
    return {"modelVersion": model_version, "evicted": registry.evict(model_version)}
