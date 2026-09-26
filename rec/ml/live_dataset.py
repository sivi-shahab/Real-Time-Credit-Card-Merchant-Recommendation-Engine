"""Export live Postgres state as a training dataset dir (continuous learning, stage 1).

`train()` only reads dataset dirs, so live feedback reaches a model by being written in
the generator's file format. Impressions still inside their observation window are left
out: `build()` takes as-of = latest impression + window, and would otherwise label an
impression as ignored before its click could arrive (ML-003).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from rec.core.models import Merchant, Promotion
from rec.ml.attribution import OBSERVATION_WINDOW
from rec.store import pg


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write(path: Path, rows) -> int:
    count = 0
    with path.open("w") as fh:
        for row in rows:
            fh.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
            count += 1
    return count


def merchant_record(m: Merchant) -> dict:
    return {"merchantId": m.merchantId, "merchantName": m.merchantName,
            "categoryCode": m.categoryCode, "cityCode": m.cityCode,
            "channel": m.channel.value, "rating": m.rating, "status": m.status}


def promotion_record(p: Promotion) -> dict:
    return {"promotionId": p.promotionId, "merchantId": p.merchantId,
            "benefitType": p.benefitType, "benefitValue": p.benefitValue,
            "minSpendMinor": p.minSpendMinor, "maxBenefitMinor": p.maxBenefitMinor,
            "eligibleCardTiers": "|".join(t.value for t in p.eligibleCardTiers),
            "eligibleCityCodes": "|".join(p.eligibleCityCodes),
            "startsAt": _iso(p.startsAt), "endsAt": _iso(p.endsAt),
            "campaignQuota": p.campaignQuota, "quotaUsed": p.quotaUsed,
            "perCustomerLimit": p.perCustomerLimit, "status": p.status}


def write_dataset(out_dir: Path, *, dataset_id: str, customers: list[dict],
                  merchants: list[Merchant], promotions: list[Promotion],
                  envelopes: list[str], impressions: list[dict],
                  interactions: list[dict]) -> dict:
    """Pure: rows in Postgres shape -> dataset dir in the generator's shape."""
    out_dir.mkdir(parents=True, exist_ok=True)
    feedback = [{"requestId": r["request_id"], "impressionId": r["impression_id"],
                 "customerId": r["customer_id"], "merchantId": r["merchant_id"],
                 "position": r["position"], "eventType": "IMPRESSION",
                 "occurredAt": _iso(r["occurred_at"])} for r in impressions]
    feedback += [{"impressionId": r["impression_id"], "customerId": r["customer_id"],
                  "merchantId": r["merchant_id"], "eventType": r["interaction_type"],
                  "occurredAt": _iso(r["occurred_at"])} for r in interactions]
    counts = {
        "customers": _write(out_dir / "customers.jsonl", (
            {"customerId": c["customer_id"], "cityCode": c["city_code"],
             "cardTier": c["card_tier"],
             "personalizationAllowed": c["personalization_allowed"]} for c in customers)),
        "merchants": _write(out_dir / "merchants.jsonl", map(merchant_record, merchants)),
        "promotions": _write(out_dir / "promotions.jsonl", map(promotion_record, promotions)),
        "replay": _write(out_dir / "replay.jsonl", envelopes),
        "feedbackEvents": _write(out_dir / "feedback_events.jsonl", feedback),
    }
    manifest = {"datasetId": dataset_id, "source": "live-postgres", "rowCounts": counts}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


async def export(out_dir: Path, *, now: datetime | None = None) -> dict:
    """Snapshot Postgres into `out_dir`. Erased customers are already deleted there."""
    # ponytail: full snapshot every run; export incrementally once volumes make it slow.
    cutoff = (now or datetime.now(UTC)) - OBSERVATION_WINDOW
    conn = await pg.pool()
    customers = [dict(r) for r in await conn.fetch(
        "SELECT customer_id, city_code, card_tier, personalization_allowed FROM customers")]
    merchants = [pg._merchant(r) for r in await conn.fetch("SELECT * FROM merchants")]
    promotions = [pg._promotion(r) for r in await conn.fetch("SELECT * FROM promotions")]
    envelopes = [r["envelope"] for r in await conn.fetch(
        "SELECT envelope::text AS envelope FROM transaction_log "
        "WHERE outcome = 'APPLIED' ORDER BY seq")]
    impressions = [dict(r) for r in await conn.fetch(
        "SELECT * FROM impressions WHERE occurred_at <= $1", cutoff)]
    interactions = [dict(r) for r in await conn.fetch(
        """SELECT i.* FROM interactions i JOIN impressions m USING (impression_id)
           WHERE m.occurred_at <= $1""", cutoff)]
    return write_dataset(out_dir, dataset_id=out_dir.name, customers=customers,
                         merchants=merchants, promotions=promotions, envelopes=envelopes,
                         impressions=impressions, interactions=interactions)
