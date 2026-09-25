"""Dataset job runner: generate -> load master data into PostgreSQL."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pandas as pd

from rec.generator.config import DatasetConfig
from rec.generator.generate import generate
from rec.settings import settings
from rec.store import pg

_events: asyncio.Queue[dict] | None = None


def event_bus() -> asyncio.Queue:
    """SSE feed for /admin/v1/events."""
    global _events
    if _events is None:
        _events = asyncio.Queue(maxsize=500)
    return _events


async def publish(event: dict) -> None:
    bus = event_bus()
    if bus.full():
        bus.get_nowait()
    await bus.put(event)


async def create_dataset_job(cfg: DatasetConfig, idempotency_key: str | None = None) -> str:
    p = await pg.pool()
    if idempotency_key:
        row = await p.fetchrow("SELECT dataset_id FROM dataset_jobs WHERE idempotency_key=$1",
                               idempotency_key)
        if row:
            return row["dataset_id"]
    dataset_id = str(uuid.uuid4())
    await p.execute(
        """INSERT INTO dataset_jobs (dataset_id, status, config, idempotency_key)
           VALUES ($1,'QUEUED',$2,$3)""",
        dataset_id, json.dumps(json.loads(cfg.model_dump_json())), idempotency_key)
    asyncio.create_task(_run(dataset_id, cfg))
    return dataset_id


async def _run(dataset_id: str, cfg: DatasetConfig) -> None:
    p = await pg.pool()
    out_dir = Path(settings.data_dir) / dataset_id
    try:
        await p.execute("UPDATE dataset_jobs SET status='RUNNING', updated_at=now() "
                        "WHERE dataset_id=$1", dataset_id)
        await publish({"type": "dataset.status", "datasetId": dataset_id, "status": "RUNNING"})
        manifest = await asyncio.to_thread(generate, cfg, out_dir)
        await load_master_data(out_dir)
        await p.execute(
            """UPDATE dataset_jobs SET status='COMPLETED', manifest=$2, updated_at=now()
               WHERE dataset_id=$1""", dataset_id, json.dumps(manifest, default=str))
        await publish({"type": "dataset.status", "datasetId": dataset_id, "status": "COMPLETED",
                       "rowCounts": manifest["rowCounts"]})
    except Exception as exc:
        await p.execute(
            "UPDATE dataset_jobs SET status='FAILED', error=$2, updated_at=now() "
            "WHERE dataset_id=$1", dataset_id, str(exc)[:1000])
        await publish({"type": "dataset.status", "datasetId": dataset_id, "status": "FAILED",
                       "error": str(exc)[:200]})


async def load_master_data(out_dir: Path) -> dict[str, int]:
    """Upsert customers/merchants/promotions so serving has a catalog."""
    p = await pg.pool()
    customers = _read(out_dir, "customers")
    customers = customers[~customers["customerId"].isin(await pg.erased_customers())]
    merchants = _read(out_dir, "merchants")
    promotions = _read(out_dir, "promotions")

    async with p.acquire() as con, con.transaction():
        await con.executemany(
            """INSERT INTO customers (customer_id, city_code, card_tier, segment,
                 personalization_allowed) VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (customer_id) DO UPDATE SET city_code=EXCLUDED.city_code,
                 card_tier=EXCLUDED.card_tier, segment=EXCLUDED.segment,
                 personalization_allowed=EXCLUDED.personalization_allowed""",
            [(r.customerId, r.cityCode, r.cardTier, r.syntheticSegment,
              bool(r.personalizationAllowed)) for r in customers.itertuples()])
        await con.executemany(
            """INSERT INTO merchants (merchant_id, merchant_name, category_code, city_code,
                 channel, rating, status) VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (merchant_id) DO UPDATE SET merchant_name=EXCLUDED.merchant_name,
                 category_code=EXCLUDED.category_code, city_code=EXCLUDED.city_code,
                 channel=EXCLUDED.channel, rating=EXCLUDED.rating, status=EXCLUDED.status,
                 version=merchants.version+1, updated_at=now()""",
            [(r.merchantId, r.merchantName, r.categoryCode, r.cityCode, r.channel,
              float(r.rating), r.status) for r in merchants.itertuples()])
        await con.executemany(
            """INSERT INTO promotions (promotion_id, merchant_id, benefit_type, benefit_value,
                 min_spend_minor, max_benefit_minor, eligible_card_tiers, eligible_city_codes,
                 starts_at, ends_at, campaign_quota, quota_used, per_customer_limit, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
               ON CONFLICT (promotion_id) DO UPDATE SET merchant_id=EXCLUDED.merchant_id,
                 benefit_type=EXCLUDED.benefit_type, benefit_value=EXCLUDED.benefit_value,
                 min_spend_minor=EXCLUDED.min_spend_minor,
                 max_benefit_minor=EXCLUDED.max_benefit_minor,
                 eligible_card_tiers=EXCLUDED.eligible_card_tiers,
                 eligible_city_codes=EXCLUDED.eligible_city_codes,
                 starts_at=EXCLUDED.starts_at, ends_at=EXCLUDED.ends_at,
                 campaign_quota=EXCLUDED.campaign_quota, quota_used=EXCLUDED.quota_used,
                 per_customer_limit=EXCLUDED.per_customer_limit, status=EXCLUDED.status,
                 version=promotions.version+1, updated_at=now()""",
            [(r.promotionId, r.merchantId, r.benefitType, float(r.benefitValue),
              int(r.minSpendMinor), int(r.maxBenefitMinor),
              [t for t in str(r.eligibleCardTiers).split("|") if t],
              [c for c in str(r.eligibleCityCodes).split("|") if c],
              pd.Timestamp(r.startsAt).to_pydatetime(), pd.Timestamp(r.endsAt).to_pydatetime(),
              int(r.campaignQuota), int(r.quotaUsed), int(r.perCustomerLimit), r.status)
             for r in promotions.itertuples()])
    return {"customers": len(customers), "merchants": len(merchants),
            "promotions": len(promotions)}


def _read(out_dir: Path, name: str) -> pd.DataFrame:
    parquet = out_dir / f"{name}.parquet"
    if parquet.exists():
        return pd.read_parquet(parquet)
    return pd.read_json(out_dir / f"{name}.jsonl", lines=True)
