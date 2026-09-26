"""Training jobs (ML-005 step 10-11). Long job -> 202 + jobId, per §12.1.

The API only queues a job. The worker process (`python -m rec.worker`, ADR-0012) claims it
with `FOR UPDATE SKIP LOCKED` and trains, so training and Optuna never compete with
serving on an API replica (threat D-7), and several workers never take the same job.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path

from rec.api.jobs import publish
from rec.ml import client as ranking_client
from rec.ml import registry
from rec.ml.train import train
from rec.obs import AUTO_SHADOW, TRAINING_JOBS
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
    return job_id


async def claim_next() -> dict | None:
    """Take the oldest queued job; a concurrent worker skips it rather than waits."""
    conn = await pg.pool()
    row = await conn.fetchrow(
        """UPDATE training_jobs SET status = 'RUNNING', updated_at = now()
           WHERE job_id = (SELECT job_id FROM training_jobs WHERE status = 'QUEUED'
                           ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING job_id, dataset_id, params""")
    return dict(row) | {"params": json.loads(row["params"])} if row else None


async def fail_stale() -> list[str]:
    """A job RUNNING past the timeout lost its worker (crash, redeploy): fail it so it is
    visible and can be re-queued, instead of RUNNING forever."""
    # ponytail: timeout, not a heartbeat; a training run longer than it is failed wrongly.
    conn = await pg.pool()
    rows = await conn.fetch(
        """UPDATE training_jobs SET status = 'FAILED', updated_at = now(),
             error = 'no worker finished it within the training timeout'
           WHERE status = 'RUNNING' AND updated_at < now() - $1::interval
           RETURNING job_id, params""",
        timedelta(hours=settings.training_timeout_hours))
    for row in rows:
        TRAINING_JOBS.labels(json.loads(row["params"]).get("trigger", "manual"), "FAILED").inc()
    return [r["job_id"] for r in rows]


async def work_once() -> str | None:
    """One worker turn: clear lost jobs, then run the next queued one, if any."""
    await fail_stale()
    job = await claim_next()
    if job is None:
        return None
    await _run(job["job_id"], job["dataset_id"], job["params"])
    return job["job_id"]


async def _run(job_id: str, dataset_id: str, params: dict) -> None:
    conn = await pg.pool()
    try:
        # SSE events reach only this process's clients; the dashboard also polls jobs.
        await publish({"type": "training.status", "jobId": job_id, "status": "RUNNING"})
        result = await asyncio.to_thread(
            train,
            Path(settings.data_dir) / dataset_id,
            out_dir=Path(settings.model_dir),
            params=params.get("xgboost"),
            num_rounds=int(params.get("numRounds", 300)),
            test_fraction=float(params.get("testFraction", 0.25)),
            tune_trials=int(params.get("tuneTrials") or 0),
            mlflow_tracking_uri=settings.mlflow_tracking_uri,
            experiment=settings.mlflow_experiment,
            exclude_customers=await pg.erased_customers(),
        )
        payload = result.to_dict()
        await registry.record_model(payload, dataset_id=dataset_id, job_id=job_id)
        await conn.execute(
            """UPDATE training_jobs SET status='COMPLETED', result=$2, updated_at=now()
               WHERE job_id=$1""", job_id, json.dumps(payload, default=str))
        TRAINING_JOBS.labels(params.get("trigger", "manual"), "COMPLETED").inc()
        await publish({"type": "training.status", "jobId": job_id, "status": "COMPLETED",
                       "modelVersion": payload["modelVersion"],
                       "approved": payload["approved"]})
        if params.get("trigger") == "auto" and payload["approved"]:
            await shadow_if_idle(payload["modelVersion"])
    except Exception as exc:
        await conn.execute(
            "UPDATE training_jobs SET status='FAILED', error=$2, updated_at=now() "
            "WHERE job_id=$1", job_id, str(exc)[:1000])
        TRAINING_JOBS.labels(params.get("trigger", "manual"), "FAILED").inc()
        await publish({"type": "training.status", "jobId": job_id, "status": "FAILED",
                       "error": str(exc)[:200]})


async def shadow_if_idle(model_version: str) -> bool:
    """Continuous learning: an approved auto-trained model goes to SHADOW, which no
    customer sees, and only while no model is serving. Replacing a CANARY/FULL model, and
    every step past SHADOW, stays an Approver's decision (ADR-0005, SEC-001)."""
    try:
        if (await registry.deployment())["mode"] not in ("BASELINE", "SHADOW"):
            AUTO_SHADOW.labels("skipped_serving").inc()
            return False
        await ranking_client.warm(model_version)
        await registry.promote(model_version, mode="SHADOW", canary_percent=0,
                               actor=AUTO_ACTOR, note="auto-retrain")
    except Exception:  # noqa: BLE001 - the job already COMPLETED; shadowing is best effort
        AUTO_SHADOW.labels("failed").inc()
        log.exception("auto-shadow of %s skipped", model_version)
        return False
    AUTO_SHADOW.labels("promoted").inc()
    await pg.audit(AUTO_ACTOR, "System", "model.promote", f"model/{model_version}",
                   outcome="AUTOMATIC", changes={"mode": "SHADOW"})
    await publish({"type": "model.shadowed", "modelVersion": model_version})
    return True


async def list_jobs(limit: int = 25) -> list[dict]:
    conn = await pg.pool()
    rows = await conn.fetch(
        """SELECT job_id, dataset_id, status, error, created_at, updated_at,
                  result->>'modelVersion' AS model_version,
                  (result->>'approved')::boolean AS approved,
                  coalesce(params->>'trigger', 'manual') AS trigger,
                  coalesce((params->>'tuneTrials')::int, 0) AS tune_trials
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
