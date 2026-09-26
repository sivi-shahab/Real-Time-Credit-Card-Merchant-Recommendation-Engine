"""Training job runner (ML-005 step 10-11). Long job -> 202 + jobId, per §12.1."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from rec.api.jobs import publish
from rec.ml import client as ranking_client
from rec.ml import registry
from rec.ml.train import train
from rec.settings import settings
from rec.store import pg

log = logging.getLogger("ml_jobs")
AUTO_ACTOR = "system:auto-retrain"


async def create_training_job(dataset_id: str, params: dict | None = None,
                              idempotency_key: str | None = None) -> str:
    conn = await pg.pool()
    if idempotency_key:
        row = await conn.fetchrow(
            "SELECT job_id FROM training_jobs WHERE idempotency_key=$1", idempotency_key)
        if row:
            return row["job_id"]
    dataset_dir = Path(settings.data_dir) / dataset_id
    if not (dataset_dir / "feedback_events.jsonl").exists() and \
            not (dataset_dir / "feedback_events.parquet").exists():
        raise FileNotFoundError(f"dataset {dataset_id} has no feedback events to train on")

    job_id = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO training_jobs (job_id, dataset_id, status, params, idempotency_key)
           VALUES ($1,$2,'QUEUED',$3,$4)""",
        job_id, dataset_id, json.dumps(params or {}), idempotency_key)
    asyncio.create_task(_run(job_id, dataset_id, params or {}))
    return job_id


async def _run(job_id: str, dataset_id: str, params: dict) -> None:
    conn = await pg.pool()
    try:
        await conn.execute("UPDATE training_jobs SET status='RUNNING', updated_at=now() "
                           "WHERE job_id=$1", job_id)
        await publish({"type": "training.status", "jobId": job_id, "status": "RUNNING"})
        result = await asyncio.to_thread(
            train,
            Path(settings.data_dir) / dataset_id,
            out_dir=Path(settings.model_dir),
            params=params.get("xgboost"),
            num_rounds=int(params.get("numRounds", 300)),
            test_fraction=float(params.get("testFraction", 0.25)),
            mlflow_tracking_uri=settings.mlflow_tracking_uri,
            experiment=settings.mlflow_experiment,
            exclude_customers=await pg.erased_customers(),
        )
        payload = result.to_dict()
        await registry.record_model(payload, dataset_id=dataset_id, job_id=job_id)
        await conn.execute(
            """UPDATE training_jobs SET status='COMPLETED', result=$2, updated_at=now()
               WHERE job_id=$1""", job_id, json.dumps(payload, default=str))
        await publish({"type": "training.status", "jobId": job_id, "status": "COMPLETED",
                       "modelVersion": payload["modelVersion"],
                       "approved": payload["approved"]})
        if params.get("trigger") == "auto" and payload["approved"]:
            await shadow_if_idle(payload["modelVersion"])
    except Exception as exc:
        await conn.execute(
            "UPDATE training_jobs SET status='FAILED', error=$2, updated_at=now() "
            "WHERE job_id=$1", job_id, str(exc)[:1000])
        await publish({"type": "training.status", "jobId": job_id, "status": "FAILED",
                       "error": str(exc)[:200]})


async def shadow_if_idle(model_version: str) -> bool:
    """Continuous learning: an approved auto-trained model goes to SHADOW, which no
    customer sees, and only while no model is serving. Replacing a CANARY/FULL model, and
    every step past SHADOW, stays an Approver's decision (ADR-0005, SEC-001)."""
    try:
        if (await registry.deployment())["mode"] not in ("BASELINE", "SHADOW"):
            return False
        await ranking_client.warm(model_version)
        await registry.promote(model_version, mode="SHADOW", canary_percent=0,
                               actor=AUTO_ACTOR, note="auto-retrain")
    except Exception:  # noqa: BLE001 - the job already COMPLETED; shadowing is best effort
        log.exception("auto-shadow of %s skipped", model_version)
        return False
    await pg.audit(AUTO_ACTOR, "System", "model.promote", f"model/{model_version}",
                   outcome="AUTOMATIC", changes={"mode": "SHADOW"})
    await publish({"type": "model.shadowed", "modelVersion": model_version})
    return True


async def list_jobs(limit: int = 25) -> list[dict]:
    conn = await pg.pool()
    rows = await conn.fetch(
        """SELECT job_id, dataset_id, status, error, created_at, updated_at,
                  result->>'modelVersion' AS model_version,
                  (result->>'approved')::boolean AS approved
           FROM training_jobs ORDER BY created_at DESC LIMIT $1""", limit)
    return [dict(r) for r in rows]


async def get_job(job_id: str) -> dict | None:
    conn = await pg.pool()
    row = await conn.fetchrow("SELECT * FROM training_jobs WHERE job_id=$1", job_id)
    if row is None:
        return None
    out = dict(row)
    out["params"] = json.loads(out["params"]) if isinstance(out["params"], str) else out["params"]
    out["result"] = json.loads(out["result"]) if isinstance(out["result"], str) else out["result"]
    return out
