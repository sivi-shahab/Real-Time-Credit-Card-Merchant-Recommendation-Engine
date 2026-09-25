"""SDD 17.2 step 14 — automatic rollback when a live model breaches its guardrail.

Every request routed to the model arm records (degraded?, model-call latency) into a
Redis hash keyed by model version AND promotion time, so a re-promotion starts clean.
A loop in each API replica evaluates it; a Redis lock makes only one replica act.
"""
from __future__ import annotations

import asyncio
import logging

from rec.ml import registry
from rec.obs import GUARDRAIL_ROLLBACKS
from rec.settings import settings
from rec.store import pg

log = logging.getLogger("guardrail")
BUCKETS_MS = (50, 100, 200, 500, 1000)  # upper bounds; anything slower lands in "inf"
LOCK_KEY = "guardrail:lock"


def _key(deployment: dict) -> str:
    promoted = deployment.get("promoted_at")
    stamp = int(promoted.timestamp()) if promoted else 0
    return f"guard:{deployment.get('model_version')}:{stamp}"


def _bucket(latency_ms: float | None) -> str:
    if latency_ms is None:
        return "lat_inf"
    for bound in BUCKETS_MS:
        if latency_ms <= bound:
            return f"lat_{bound}"
    return "lat_inf"


async def record(r, deployment: dict, *, degraded: bool, latency_ms: float | None) -> None:
    key = _key(deployment)
    async with r.pipeline(transaction=False) as pipe:
        pipe.hincrby(key, "requests", 1)
        if degraded:
            pipe.hincrby(key, "degraded", 1)
        pipe.hincrby(key, _bucket(latency_ms), 1)
        pipe.expire(key, 7 * 24 * 3600)
        await pipe.execute()


def p95_upper_ms(stats: dict[str, int]) -> float | None:
    """Upper bound of the bucket holding the 95th percentile (conservative)."""
    n = sum(v for k, v in stats.items() if k.startswith("lat_"))
    if not n:
        return None
    seen = 0
    for bound in BUCKETS_MS:
        seen += stats.get(f"lat_{bound}", 0)
        if seen >= 0.95 * n:
            return float(bound)
    return float("inf")


def evaluate(stats: dict[str, int]) -> list[str]:
    """Pure: the reasons this arm breaches its guardrail (empty = healthy / not enough data)."""
    n = stats.get("requests", 0)
    if n < settings.guardrail_min_requests:
        return []
    reasons = []
    rate = stats.get("degraded", 0) / n
    if rate > settings.guardrail_max_degraded_rate:
        reasons.append(f"degraded rate {rate:.1%} > {settings.guardrail_max_degraded_rate:.1%}")
    p95 = p95_upper_ms(stats)
    if p95 is not None and p95 > settings.guardrail_max_p95_ms:
        reasons.append(f"model p95 <= {p95} ms bucket > {settings.guardrail_max_p95_ms} ms")
    return reasons


async def status(r) -> dict:
    deployment = await registry.deployment()
    live = deployment.get("mode") in ("CANARY", "FULL") and deployment.get("model_version")
    stats = {k: int(v) for k, v in (await r.hgetall(_key(deployment))).items()} if live else {}
    return {"mode": deployment.get("mode"), "modelVersion": deployment.get("model_version"),
            "watching": bool(live), "stats": stats, "p95UpperMs": p95_upper_ms(stats),
            "breaches": evaluate(stats),
            "thresholds": {"minRequests": settings.guardrail_min_requests,
                           "maxDegradedRate": settings.guardrail_max_degraded_rate,
                           "maxP95Ms": settings.guardrail_max_p95_ms}}


async def check(store) -> dict | None:
    """One evaluation. Returns the new deployment if it rolled back."""
    s = await status(store.r)
    if not s["breaches"]:
        return None
    if not await store.r.set(LOCK_KEY, "1", nx=True, ex=settings.guardrail_interval_seconds):
        return None  # another replica is acting on this
    note = "guardrail: " + "; ".join(s["breaches"])
    deployment = await registry.rollback("system:guardrail", note=note)
    await store.invalidate_all_recommendations()
    await pg.audit("system:guardrail", "System", "model.rollback",
                   f"model/{s['modelVersion']}", outcome="AUTOMATIC",
                   changes={"breaches": s["breaches"], "stats": s["stats"],
                            "now": deployment.get("mode")})
    GUARDRAIL_ROLLBACKS.inc()
    log.warning("guardrail rolled back %s: %s", s["modelVersion"], note)
    return deployment


async def loop(store) -> None:
    while True:
        await asyncio.sleep(settings.guardrail_interval_seconds)
        try:
            await check(store)
        except Exception:  # noqa: BLE001 - a broken check must not kill the API
            log.exception("guardrail check failed")
