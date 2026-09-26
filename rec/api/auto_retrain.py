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
from rec.ml import live_dataset, registry
from rec.ml.attribution import OBSERVATION_WINDOW
from rec.obs import AUTO_RETRAIN_RUNS, LIVE_EXPORTS_PRUNED
from rec.settings import settings
from rec.store import pg

log = logging.getLogger("auto_retrain")
LOCK_KEY = "auto_retrain:lock"
_sleep = asyncio.sleep  # the loop's clock; a test swaps this, not asyncio's


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
        AUTO_RETRAIN_RUNS.labels("below_threshold").inc()
        return None
    ttl = max(60, int(settings.auto_retrain_interval_hours * 3600))
    if not await store.r.set(LOCK_KEY, "1", nx=True, ex=ttl):
        AUTO_RETRAIN_RUNS.labels("locked").inc()
        return None  # another replica is on it

    dataset_id = f"live-{now:%Y%m%d%H%M%S}"
    await live_dataset.export(Path(settings.data_dir) / dataset_id, now=now)
    job_id = await ml_jobs.create_training_job(dataset_id, {"trigger": "auto"})
    await pg.audit(ml_jobs.AUTO_ACTOR, "System", "training.start", f"trainingJob/{job_id}",
                   outcome="AUTOMATIC", changes={"datasetId": dataset_id, "newImpressions": new})
    AUTO_RETRAIN_RUNS.labels("queued").inc()
    log.info("auto-retrain queued %s on %s (%d new impressions)", job_id, dataset_id, new)
    await prune_exports()
    return job_id


async def prune_exports() -> list[str]:
    """I-9: each export copies customer behaviour to disk. Keep the newest few, plus any
    a job is still reading and any behind a model that serves or could be rolled back to
    (it must stay reproducible). Deletions are audited."""
    deployment = await registry.deployment()
    conn = await pg.pool()
    pinned = frozenset(r["dataset_id"] for r in await conn.fetch(
        """SELECT dataset_id FROM models WHERE model_version = ANY($1)
           UNION SELECT dataset_id FROM training_jobs WHERE status IN ('QUEUED', 'RUNNING')""",
        [v for v in (deployment.get("model_version"), deployment.get("previous_version")) if v]))
    removed = await asyncio.to_thread(live_dataset.prune, Path(settings.data_dir),
                                      keep=settings.auto_retrain_keep_exports, pinned=pinned)
    if removed:
        LIVE_EXPORTS_PRUNED.inc(len(removed))
        await pg.audit(ml_jobs.AUTO_ACTOR, "System", "dataset.prune", "datasets/live",
                       outcome="AUTOMATIC",
                       changes={"removed": removed, "keep": settings.auto_retrain_keep_exports,
                                "pinned": sorted(pinned)})
    return removed


async def loop(store) -> None:
    """Checks every interval while enabled; while off (0) it looks again each minute, so
    an approved change turns it on without a restart (ADR-0011)."""
    while True:
        hours = settings.auto_retrain_interval_hours
        await _sleep(hours * 3600 if hours > 0 else 60)
        if settings.auto_retrain_interval_hours <= 0:
            continue
        try:
            await check(store)
        except Exception:  # noqa: BLE001 - a failed run must not kill the API
            AUTO_RETRAIN_RUNS.labels("failed").inc()
            log.exception("auto-retrain check failed")
