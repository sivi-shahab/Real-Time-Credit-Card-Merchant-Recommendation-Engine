"""Model registry and deployment state (SDD 12, ML-006, 17.2).

The deployment row is the single serving decision. Promotion requires an approved
model AND a matching feature schema — a high offline score is not sufficient.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from rec.ml.vectorize import FEATURE_SCHEMA_VERSION as VECTOR_SCHEMA_VERSION
from rec.store import pg

BASELINE_VERSION = "baseline-1.0.0"
MODES = ("BASELINE", "SHADOW", "CANARY", "FULL")


class PromotionRefused(Exception):
    pass


async def record_model(result: dict, *, dataset_id: str, job_id: str | None) -> None:
    conn = await pg.pool()
    await conn.execute(
        """INSERT INTO models (model_version, dataset_id, job_id, feature_schema_version,
             trainer_version, artifact_path, mlflow_run_id, approved, metrics,
             baseline_metrics, segment_metrics, gates, lineage)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
           ON CONFLICT (model_version) DO NOTHING""",
        result["modelVersion"], dataset_id, job_id, result["featureSchemaVersion"],
        result["trainerVersion"], result["artifacts"]["model"], result.get("mlflowRunId"),
        bool(result["approved"]), json.dumps(result["metrics"]),
        json.dumps(result["baselineMetrics"]), json.dumps(result["segmentMetrics"], default=str),
        json.dumps(result["gates"]), json.dumps(result["datasetLineage"], default=str))


async def list_models(limit: int = 50) -> list[dict]:
    conn = await pg.pool()
    rows = await conn.fetch(
        """SELECT model_version, dataset_id, job_id, feature_schema_version, trainer_version,
                  mlflow_run_id, approved, metrics, baseline_metrics, gates, lineage, created_at
           FROM models ORDER BY created_at DESC LIMIT $1""", limit)
    return [_decode(dict(r)) for r in rows]


async def get_model(model_version: str) -> dict | None:
    conn = await pg.pool()
    row = await conn.fetchrow("SELECT * FROM models WHERE model_version=$1", model_version)
    return _decode(dict(row)) if row else None


def _decode(row: dict) -> dict:
    for key in ("metrics", "baseline_metrics", "segment_metrics", "gates", "lineage"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


async def deployment() -> dict:
    conn = await pg.pool()
    row = await conn.fetchrow("SELECT * FROM model_deployment WHERE id=1")
    return dict(row) if row else {"mode": "BASELINE", "model_version": None,
                                  "canary_percent": 0, "previous_version": None}


async def promote(model_version: str, *, mode: str, canary_percent: int, actor: str,
                  note: str | None = None) -> dict:
    """SHADOW/CANARY/FULL all require the gates to have passed; only the blast radius
    differs. Rolling back to BASELINE never requires approval."""
    if mode not in MODES:
        raise PromotionRefused(f"unknown mode {mode}")
    model = await get_model(model_version)
    if model is None:
        raise PromotionRefused(f"unknown model {model_version}")
    if not model["approved"]:
        failed = [g["name"] for g in model["gates"] if not g["passed"]]
        raise PromotionRefused(f"model failed evaluation gates: {failed}")
    if model["feature_schema_version"] != VECTOR_SCHEMA_VERSION:
        raise PromotionRefused(
            f"feature schema {model['feature_schema_version']} != serving "
            f"{VECTOR_SCHEMA_VERSION}; retrain before promoting")
    if mode == "CANARY" and not 1 <= canary_percent <= 100:
        raise PromotionRefused("canary mode needs canaryPercent between 1 and 100")

    current = await deployment()
    conn = await pg.pool()
    row = await conn.fetchrow(
        """UPDATE model_deployment SET mode=$1, model_version=$2, previous_version=$3,
             canary_percent=$4, promoted_by=$5, promoted_at=now(), note=$6
           WHERE id=1 RETURNING *""",
        mode, model_version,
        current["model_version"] if current["model_version"] != model_version
        else current["previous_version"],
        canary_percent if mode == "CANARY" else (100 if mode == "FULL" else 0),
        actor, note)
    return dict(row)


async def rollback(actor: str, *, note: str | None = None) -> dict:
    """Return to the previous model, or to the baseline when there is none."""
    current = await deployment()
    target = current.get("previous_version")
    conn = await pg.pool()
    if target:
        row = await conn.fetchrow(
            """UPDATE model_deployment SET mode='FULL', model_version=$1,
                 previous_version=NULL, canary_percent=100, promoted_by=$2,
                 promoted_at=now(), note=$3 WHERE id=1 RETURNING *""",
            target, actor, note or "rollback to previous model")
    else:
        row = await conn.fetchrow(
            """UPDATE model_deployment SET mode='BASELINE', model_version=NULL,
                 previous_version=NULL, canary_percent=0, promoted_by=$1,
                 promoted_at=now(), note=$2 WHERE id=1 RETURNING *""",
            actor, note or "rollback to baseline")
    return dict(row)


def in_canary(customer_id: str, percent: int) -> bool:
    """Deterministic per customer: a customer does not flip between rankers on refresh."""
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.sha256(customer_id.encode()).digest()
    return (int.from_bytes(digest[:4], "big") % 100) < percent


async def record_shadow(request_id: str, customer_id: str, model_version: str,
                        served_source: str, rank_agreement: float | None,
                        top1_agreement: bool | None, latency_ms: float | None) -> None:
    conn = await pg.pool()
    await conn.execute(
        """INSERT INTO shadow_evaluations (request_id, customer_id, model_version,
             served_source, rank_agreement, top1_agreement, model_latency_ms, occurred_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (request_id) DO NOTHING""",
        request_id, customer_id, model_version, served_source, rank_agreement,
        top1_agreement, latency_ms, datetime.now(UTC))


async def shadow_summary(limit_hours: int = 24) -> dict:
    conn = await pg.pool()
    row = await conn.fetchrow(
        """SELECT count(*) AS comparisons,
                  avg(rank_agreement) AS avg_rank_agreement,
                  avg(CASE WHEN top1_agreement THEN 1.0 ELSE 0.0 END) AS top1_agreement_rate,
                  percentile_disc(0.95) WITHIN GROUP (ORDER BY model_latency_ms)
                    AS model_latency_p95
           FROM shadow_evaluations
           WHERE occurred_at > now() - ($1 || ' hours')::interval""", str(limit_hours))
    out = dict(row)
    return {k: (float(v) if isinstance(v, (int, float)) and v is not None else v)
            for k, v in out.items()}
