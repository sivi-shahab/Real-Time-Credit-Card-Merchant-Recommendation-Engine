"""Background worker (ADR-0012): everything that learns runs here, off the API replicas.

    python -m rec.worker

- training jobs the API queued (`ml_jobs.work_once`), one at a time per worker;
- the scheduled uplift report (`UPLIFT_REPORT_INTERVAL_HOURS`, 0 = off);
- the auto-retrain and online-bandit loops (ADR-0007), which moved here from the API;
- the approved learning settings (ADR-0011), re-read like on every API replica.

Several workers are safe: jobs are claimed with SKIP LOCKED, and the report, auto-retrain
and bandit passes each take a Redis lock. Metrics are served on `WORKER_METRICS_PORT`.
"""
from __future__ import annotations

import asyncio
import logging

from prometheus_client import start_http_server

from rec.api import auto_retrain, learning_settings, ml_jobs, serving_log
from rec.ml import bandit, uplift
from rec.obs import setup_logging
from rec.settings import settings
from rec.store import pg
from rec.store.redis_store import OnlineStore

log = logging.getLogger("worker")


async def training_loop() -> None:
    while True:
        try:
            if await ml_jobs.work_once() is None:
                await asyncio.sleep(settings.training_poll_seconds)
        except Exception:  # noqa: BLE001 - a broken turn must not stop the queue
            log.exception("training turn failed")
            await asyncio.sleep(settings.training_poll_seconds)


async def uplift_loop(store: OnlineStore) -> None:
    """Every interval while on; while off (0) it looks again each minute."""
    while True:
        hours = settings.uplift_report_interval_hours
        await asyncio.sleep(hours * 3600 if hours > 0 else 60)
        if settings.uplift_report_interval_hours > 0:
            log.info("uplift report: %s", await uplift.scheduled_report(store))


async def serving_log_prune_loop() -> None:
    """Keep the analytics serving log within its retention; hourly is plenty."""
    while True:
        try:
            removed = await serving_log.prune()
            if removed:
                log.info("serving log: pruned %s responses past retention", removed)
        except Exception:  # noqa: BLE001 - retried next hour
            log.exception("serving log prune failed")
        await asyncio.sleep(3600)


async def run() -> None:
    setup_logging()
    start_http_server(settings.worker_metrics_port)
    store = OnlineStore()
    await pg.pool()
    await learning_settings.refresh(force=True)
    log.info("worker started: training queue, uplift report, auto-retrain, bandit")
    try:
        await asyncio.gather(training_loop(), uplift_loop(store), auto_retrain.loop(store),
                             bandit.loop(store), learning_settings.loop(),
                             serving_log_prune_loop())
    finally:
        await store.close()
        await pg.close()


if __name__ == "__main__":
    asyncio.run(run())
