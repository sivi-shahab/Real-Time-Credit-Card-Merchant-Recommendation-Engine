"""HTTP client for the Ranking Service. Every failure mode collapses to "no scores",
which the serving path turns into the baseline ranking (SERV-003, AC-004)."""
from __future__ import annotations

import base64
import logging

import httpx
import numpy as np

from rec.ml.vectorize import FEATURE_NAMES
from rec.ml.vectorize import FEATURE_SCHEMA_VERSION as VECTOR_SCHEMA_VERSION
from rec.settings import settings

log = logging.getLogger("ranking-client")


class RankingUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.ranking_service_url,
            headers={"authorization": f"Bearer {settings.ranking_service_token}"}
            if settings.ranking_service_token else None,
            timeout=httpx.Timeout(settings.ranking_timeout_ms / 1000),
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def warm(model_version: str, sha256: str | None, *, timeout_s: float = 30.0) -> None:
    """Load the artifact before any traffic sees it. A cold load can exceed the serving
    timeout and degrade the first requests to baseline for no good reason. The ranking
    service refuses an artifact whose SHA-256 differs from the recorded one (T-4)."""
    if not sha256:
        raise RankingUnavailable("ARTIFACT_DIGEST_MISSING")
    try:
        response = await client().post(f"/v1/models/{model_version}/warm",
                                      json={"modelSha256": sha256},
                                      timeout=httpx.Timeout(timeout_s))
    except httpx.HTTPError as exc:
        raise RankingUnavailable(f"RANKING_UNREACHABLE:{type(exc).__name__}")
    if response.status_code == 412:
        raise RankingUnavailable("ARTIFACT_INTEGRITY")
    if response.status_code >= 400:
        raise RankingUnavailable(f"RANKING_WARM_HTTP_{response.status_code}")


async def score(model_version: str, candidates: list[dict[str, float]], *,
                sha256: str | None) -> tuple[list[float], float]:
    """Returns (scores, inferenceMs). Raises RankingUnavailable on any failure."""
    if not candidates:
        return [], 0.0
    if not sha256:  # a model recorded before T-4 has no digest: fail closed, serve baseline
        raise RankingUnavailable("ARTIFACT_DIGEST_MISSING")
    try:
        rows = np.asarray([[c[name] for name in FEATURE_NAMES] for c in candidates],
                          dtype="<f4")
    except KeyError as exc:
        raise RankingUnavailable(f"MISSING_FEATURE:{exc.args[0]}")
    try:
        response = await client().post("/v1/score", json={
            "modelVersion": model_version,
            "modelSha256": sha256,
            "featureSchemaVersion": VECTOR_SCHEMA_VERSION,
            "count": len(candidates),
            "rows": base64.b64encode(rows.tobytes()).decode(),
        })
    except httpx.TimeoutException:
        raise RankingUnavailable("RANKING_TIMEOUT")
    except httpx.HTTPError as exc:
        raise RankingUnavailable(f"RANKING_TRANSPORT:{type(exc).__name__}")
    if response.status_code == 409:
        raise RankingUnavailable("FEATURE_SCHEMA_MISMATCH")
    if response.status_code == 412:
        raise RankingUnavailable("ARTIFACT_INTEGRITY")
    if response.status_code >= 400:
        raise RankingUnavailable(f"RANKING_HTTP_{response.status_code}")
    body = response.json()
    scores = body.get("scores") or []
    if len(scores) != len(candidates):
        raise RankingUnavailable("RANKING_LENGTH_MISMATCH")
    return [float(s) for s in scores], float(body.get("inferenceMs", 0.0))
