"""Continuous learning, stage 1 — retrain on live feedback on a schedule.

Every `auto_retrain_interval_hours` one API replica (Redis lock) counts the impressions
that became observable since the last automatic job. Past the threshold it exports
Postgres as a dataset and queues an ordinary training job: same builder, same gates, same
registry. `ml_jobs.shadow_if_idle` takes an approved result as far as SHADOW, no further.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from rec.api import ml_jobs
from rec.ml import live_dataset
from rec.ml.attribution import OBSERVATION_WINDOW
from rec.settings import settings
from rec.store import pg

log = logging.getLogger("auto_retrain")
LOCK_KEY = "auto_retrain:lock"


async def check(store, *, now: datetime | None = None) -> str | None:
    """One evaluation. Returns the queued job id, or None when there is nothing to learn."""
    now = now or datetime.now(UTC)
    conn = await pg.pool()
    last = await conn.fetchval(
        "SELECT max(created_at) FROM training_jobs WHERE params->>'trigger' = 'auto'")
    # The last export took impressions up to last - window; count what is observable since.
    new = await conn.fetchval(
        """SELECT count(*) FROM impressions
           WHERE occurred_at <= $1 AND ($2::timestamptz IS NULL OR occurred_at > $2)""",
        now - OBSERVATION_WINDOW, last - OBSERVATION_WINDOW if last else None)
    if new < settings.auto_retrain_min_new_impressions:
        return None
    ttl = max(60, int(settings.auto_retrain_interval_hours * 3600))
    if not await store.r.set(LOCK_KEY, "1", nx=True, ex=ttl):
        return None  # another replica is on it

    dataset_id = f"live-{now:%Y%m%d%H%M%S}"
    # ponytail: one full snapshot dir per run; prune old live-* dirs when disk matters.
    await live_dataset.export(Path(settings.data_dir) / dataset_id, now=now)
    job_id = await ml_jobs.create_training_job(dataset_id, {"trigger": "auto"})
    await pg.audit(ml_jobs.AUTO_ACTOR, "System", "training.start", f"trainingJob/{job_id}",
                   outcome="AUTOMATIC", changes={"datasetId": dataset_id, "newImpressions": new})
    log.info("auto-retrain queued %s on %s (%d new impressions)", job_id, dataset_id, new)
    return job_id


async def loop(store) -> None:
    while True:
        await asyncio.sleep(settings.auto_retrain_interval_hours * 3600)
        try:
            await check(store)
        except Exception:  # noqa: BLE001 - a failed run must not kill the API
            log.exception("auto-retrain check failed")
