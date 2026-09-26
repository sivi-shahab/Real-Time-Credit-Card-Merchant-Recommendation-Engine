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
BANDIT_VERSION = "online-ucb"  # continuous learning stage 2, shadow only (ADR-0007)
MODES = ("BASELINE", "SHADOW", "CANARY", "FULL")


class PromotionRefused(Exception):
    pass


async def record_model(result: dict, *, dataset_id: str, job_id: str | None) -> None:
    conn = await pg.pool()
    await conn.execute(
        """INSERT INTO models (model_version, dataset_id, job_id, feature_schema_version,
             trainer_version, artifact_path, mlflow_run_id, approved, metrics,
             baseline_metrics, segment_metrics, gates, lineage, artifacts, artifact_sha256)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
           ON CONFLICT (model_version) DO NOTHING""",
        result["modelVersion"], dataset_id, job_id, result["featureSchemaVersion"],
        result["trainerVersion"], result["artifacts"]["model"], result.get("mlflowRunId"),
        bool(result["approved"]), json.dumps(result["metrics"]),
        json.dumps(result["baselineMetrics"]), json.dumps(result["segmentMetrics"], default=str),
        json.dumps(result["gates"]), json.dumps(result["datasetLineage"], default=str),
        json.dumps({k: result["artifacts"].get(k)  # the small ones; the model is a file
                    for k in ("featureImportanceGain", "positionBias")}),
        result["artifacts"].get("modelSha256"))


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
    for key in ("metrics", "baseline_metrics", "segment_metrics", "gates", "lineage",
                "artifacts"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


async def deployment() -> dict:
    conn = await pg.pool()
    # The digest rides along so the ranking service can verify the file it loads (T-4).
    row = await conn.fetchrow(
        """SELECT d.*, m.artifact_sha256 FROM model_deployment d
           LEFT JOIN models m ON m.model_version = d.model_version WHERE d.id=1""")
    return dict(row) if row else {"mode": "BASELINE", "model_version": None,
                                  "canary_percent": 0, "previous_version": None,
                                  "artifact_sha256": None}


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
    # Rollback restores previous_version as FULL, so it may only ever hold a model that
    # was serving customers. A SHADOW model never was: recording it would let a rollback
    # put an unreviewed shadow candidate on full traffic.
    was_serving = current["mode"] in ("CANARY", "FULL") \
        and current["model_version"] != model_version
    conn = await pg.pool()
    row = await conn.fetchrow(
        """UPDATE model_deployment SET mode=$1, model_version=$2, previous_version=$3,
             canary_percent=$4, promoted_by=$5, promoted_at=now(), note=$6
           WHERE id=1 RETURNING *""",
        mode, model_version,
        current["model_version"] if was_serving else current["previous_version"],
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


def rank_agreement(a: list[str], b: list[str]) -> float:
    """Share of positions where two orderings agree on the same merchant."""
    if not a or not b:
        return 0.0
    pairs = min(len(a), len(b))
    return sum(1 for i in range(pairs) if a[i] == b[i]) / pairs


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


async def shadow_summary(limit_hours: int = 24, model_version: str | None = None) -> dict:
    """One model version, or by default every shadow model except the online bandit,
    which runs beside every request and would drown the promoted candidate's numbers."""
    conn = await pg.pool()
    row = await conn.fetchrow(
        """SELECT count(*) AS comparisons,
                  avg(rank_agreement) AS avg_rank_agreement,
                  avg(CASE WHEN top1_agreement THEN 1.0 ELSE 0.0 END) AS top1_agreement_rate,
                  percentile_disc(0.95) WITHIN GROUP (ORDER BY model_latency_ms)
                    AS model_latency_p95
           FROM shadow_evaluations
           WHERE occurred_at > now() - ($1 || ' hours')::interval
             AND CASE WHEN $2::text IS NULL THEN model_version <> $3
                      ELSE model_version = $2 END""",
        str(limit_hours), model_version, BANDIT_VERSION)
    out = dict(row)
    return {k: (float(v) if isinstance(v, (int, float)) and v is not None else v)
            for k, v in out.items()}
