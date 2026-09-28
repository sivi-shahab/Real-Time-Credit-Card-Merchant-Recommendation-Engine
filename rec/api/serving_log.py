"""What each customer was actually served, for analytics (Superset) — not for serving.

The request path only appends to an in-process buffer; a background task writes the
buffer in one transaction every `SERVING_LOG_FLUSH_SECONDS`, so logging never enters the
customer's latency. A crash loses at most that interval of rows: this is analytics, the
served slates that feedback is checked against live in Redis (S-5).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from rec.core.models import RecommendationResponse
from rec.obs import SERVING_LOG_DROPPED
from rec.settings import settings
from rec.store import pg

log = logging.getLogger("serving-log")

_buffer: list[tuple[tuple, list[tuple]]] = []


def record(response: RecommendationResponse, *, city_code: str | None,
           channel: str | None) -> None:
    """Queue one response served to a customer. Never raises, never waits."""
    if len(_buffer) >= settings.serving_log_max_buffer:
        SERVING_LOG_DROPPED.inc()  # the database is behind; serving must not wait for it
        return
    log_id = uuid.uuid4()
    cold_start = bool(response.stale)
    row = (log_id, response.requestId, response.customerId, datetime.now(UTC),
           response.modelVersion, response.source,
           response.source != "FALLBACK" and not cold_start, cold_start,
           city_code, channel, len(response.recommendations))
    items = [(log_id, n, item.merchantId, item.categoryCode, list(item.reasonCodes),
              (item.promotion or {}).get("promotionId"))
             for n, item in enumerate(response.recommendations)]
    _buffer.append((row, items))


async def flush() -> int:
    """Write what is buffered; returns the number of responses written."""
    if not _buffer:
        return 0
    batch = _buffer[:]  # rows appended while this awaits stay for the next flush
    conn = await pg.pool()
    async with conn.acquire() as con, con.transaction():
        await con.executemany(
            """INSERT INTO recommendation_log (log_id, request_id, customer_id, served_at,
                 model_version, source, personalized, cold_start, city_code, channel,
                 item_count) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            [row for row, _ in batch])
        await con.executemany(
            """INSERT INTO recommendation_log_items (log_id, position, merchant_id,
                 category_code, reason_codes, promotion_id) VALUES ($1,$2,$3,$4,$5,$6)""",
            [item for _, items in batch for item in items])
    del _buffer[:len(batch)]  # only once written: a failed flush keeps them
    return len(batch)


async def loop() -> None:
    while True:
        await asyncio.sleep(settings.serving_log_flush_seconds)
        try:
            await flush()
        except Exception:  # noqa: BLE001 - analytics must not take serving down
            log.exception("serving log flush failed; rows kept for the next try")


async def prune() -> int:
    """Drop rows past `SERVING_LOG_RETENTION_DAYS` (items go with them)."""
    conn = await pg.pool()
    tag = await conn.execute(
        "DELETE FROM recommendation_log WHERE served_at < now() - make_interval(days => $1)",
        settings.serving_log_retention_days)
    return int(tag.split()[-1])
