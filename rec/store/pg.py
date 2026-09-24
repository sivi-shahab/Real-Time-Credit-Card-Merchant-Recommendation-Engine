"""PostgreSQL access. Thin asyncpg helpers; no ORM (nothing here needs one)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import asyncpg

from rec.core.models import CardTier, Channel, Merchant, Promotion
from rec.settings import settings

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.postgres_dsn, min_size=2,
                                         max_size=settings.pg_max_connections)
        await _migrate(_pool)
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


MIGRATION_LOCK = 8_421_337  # arbitrary, stable


async def _migrate(p: asyncpg.Pool) -> None:
    """Services start together; an advisory lock serialises the schema apply.
    Without it concurrent GRANT/REVOKE hit 'tuple concurrently updated'."""
    schema = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    async with p.acquire() as con:
        await con.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK)
        try:
            await con.execute(schema.read_text())
        finally:
            await con.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK)


# ------------------------------------------------------------------ catalog


def _merchant(row) -> Merchant:
    return Merchant(
        merchantId=row["merchant_id"], merchantName=row["merchant_name"],
        categoryCode=row["category_code"], cityCode=row["city_code"],
        channel=Channel(row["channel"]), rating=float(row["rating"]), status=row["status"],
    )


def _promotion(row) -> Promotion:
    return Promotion(
        promotionId=row["promotion_id"], merchantId=row["merchant_id"],
        benefitType=row["benefit_type"], benefitValue=float(row["benefit_value"]),
        minSpendMinor=row["min_spend_minor"], maxBenefitMinor=row["max_benefit_minor"],
        eligibleCardTiers=[CardTier(t) for t in row["eligible_card_tiers"]],
        eligibleCityCodes=list(row["eligible_city_codes"]),
        startsAt=row["starts_at"], endsAt=row["ends_at"],
        campaignQuota=row["campaign_quota"], quotaUsed=row["quota_used"],
        perCustomerLimit=row["per_customer_limit"], status=row["status"],
    )


async def merchants(city_code: str | None = None, search: str | None = None,
                    limit: int = 1000, offset: int = 0) -> list[Merchant]:
    p = await pool()
    rows = await p.fetch(
        """SELECT * FROM merchants
           WHERE ($1::text IS NULL OR city_code = $1)
             AND ($2::text IS NULL OR merchant_name ILIKE '%'||$2||'%'
                  OR merchant_id ILIKE '%'||$2||'%')
           ORDER BY merchant_id LIMIT $3 OFFSET $4""",
        city_code, search, limit, offset,
    )
    return [_merchant(r) for r in rows]


async def merchant_city_map() -> dict[str, str]:
    p = await pool()
    return {r["merchant_id"]: r["city_code"] for r in await p.fetch(
        "SELECT merchant_id, city_code FROM merchants")}


async def active_promotions(now: datetime) -> list[Promotion]:
    p = await pool()
    rows = await p.fetch(
        """SELECT * FROM promotions
           WHERE status = 'ACTIVE' AND starts_at <= $1 AND ends_at >= $1
           ORDER BY promotion_id""", now)
    return [_promotion(r) for r in rows]


async def promotions(limit: int = 500, offset: int = 0) -> list[Promotion]:
    p = await pool()
    rows = await p.fetch("SELECT * FROM promotions ORDER BY promotion_id LIMIT $1 OFFSET $2",
                         limit, offset)
    return [_promotion(r) for r in rows]


async def customer(customer_id: str):
    p = await pool()
    return await p.fetchrow("SELECT * FROM customers WHERE customer_id = $1", customer_id)


async def customer_redemption_counts(customer_id: str) -> dict[str, int]:
    """Per-customer promo usage, from recorded redemptions (PROMO-001)."""
    p = await pool()
    rows = await p.fetch(
        """SELECT i.merchant_id, count(*) AS n FROM interactions i
           WHERE i.customer_id = $1 AND i.interaction_type = 'REDEMPTION'
           GROUP BY i.merchant_id""", customer_id)
    return {r["merchant_id"]: r["n"] for r in rows}


# ------------------------------------------------------------------ logs


async def log_transaction(env: dict, outcome: str, reject_code: str | None = None,
                          reject_detail: str | None = None) -> None:
    """One INSERT per ingested event. This is the ingestion throughput ceiling
    (~120 eps locally); reaching the 10k TPS NFR needs a batched COPY sink or an
    async archive writer, which is Fase 5 load-hardening work, not Fase 1-3."""
    payload = env.get("payload", {})
    p = await pool()
    await p.execute(
        """INSERT INTO transaction_log (event_id, transaction_id, customer_id, merchant_id,
             txn_type, amount_minor, occurred_at, outcome, reject_code, reject_detail,
             correlation_id, envelope)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
           ON CONFLICT (event_id) DO NOTHING""",
        env.get("eventId"), payload.get("transactionId"), payload.get("customerId"),
        payload.get("merchantId"), payload.get("transactionType"),
        int(payload.get("amountMinor") or 0), _ts(payload.get("occurredAt")), outcome,
        reject_code, reject_detail, env.get("correlationId"), json.dumps(env, default=str),
    )


def _ts(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def audit(actor: str, actor_role: str, action: str, resource: str,
                outcome: str = "SUCCESS", changes: dict | None = None,
                trace_id: str | None = None) -> None:
    p = await pool()
    await p.execute(
        """INSERT INTO audit_events (actor, actor_role, action, resource, outcome, changes,
             trace_id) VALUES ($1,$2,$3,$4,$5,$6,$7)""",
        actor, actor_role, action, resource, outcome,
        json.dumps(changes, default=str) if changes else None, trace_id,
    )
