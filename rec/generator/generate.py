"""SYN-002..006 — reproducible synthetic dataset generation.

Determinism rules: one seeded numpy Generator, no wall-clock reads inside the
data path, sorted output ordering, stable float formatting.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (
    CARD_TIERS,
    CATEGORIES,
    CATEGORY_TICKET_MINOR,
    CITIES,
    DatasetConfig,
)

SCHEMA_VERSION = "1.0.0"
_NS = uuid.UUID("6f1a0f9a-0000-4000-8000-000000000000")  # deterministic UUID namespace


def _uuid(*parts) -> str:
    return str(uuid.uuid5(_NS, "|".join(str(p) for p in parts)))


def _zipf(rng: np.random.Generator, n: int, a: float = 1.2) -> np.ndarray:
    """Long-tail popularity weights (SYN-003)."""
    w = 1.0 / np.power(np.arange(1, n + 1), a)
    idx = rng.permutation(n)
    return (w / w.sum())[idx]


def _ns(cfg: DatasetConfig, value: str) -> str:
    return f"{cfg.idNamespace}-{value}" if cfg.idNamespace else value


def _salt(cfg: DatasetConfig) -> str:
    """Event and transaction IDs must be namespaced too — otherwise two datasets
    sharing a seed collide in downstream deduplication (SIM-002)."""
    return f"{cfg.idNamespace}|{cfg.seed}"


def generate(cfg: DatasetConfig, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(cfg.seed)
    ref = cfg.referenceTime.astimezone(UTC)

    customers = _customers(rng, cfg)
    merchants = _merchants(rng, cfg)
    promotions = _promotions(rng, cfg, merchants, ref)
    transactions, feedback = _transactions(rng, cfg, customers, merchants, promotions, ref)

    tables = {
        "customers": customers,
        "merchants": merchants,
        "promotions": promotions,
        "transactions": transactions,
        "feedback_events": feedback,
    }
    files = _write(tables, cfg, out_dir)
    quality = _quality_report(tables, cfg)
    manifest = {
        "datasetId": _uuid("dataset", _salt(cfg), cfg.model_dump_json()),
        "generatorVersion": cfg.generatorVersion,
        "schemaVersion": SCHEMA_VERSION,
        "config": json.loads(cfg.model_dump_json()),
        "rowCounts": {k: len(v) for k, v in tables.items()},
        "files": files,
        "qualityReport": quality,
        "distributionSummary": _distribution_summary(tables),
    }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True, default=str)
    (out_dir / "manifest.json").write_text(manifest_text)
    manifest["manifestChecksum"] = hashlib.sha256(manifest_text.encode()).hexdigest()
    return manifest


# ---------------------------------------------------------------- entities


def _customers(rng: np.random.Generator, cfg: DatasetConfig) -> pd.DataFrame:
    n = cfg.customerCount
    city_w = _zipf(rng, len(CITIES), 0.7)
    prefs = [
        sorted(rng.choice(CATEGORIES, size=int(rng.integers(2, 5)), replace=False).tolist())
        for _ in range(n)
    ]
    return pd.DataFrame(
        {
            "customerId": [_ns(cfg, f"C{i:07d}") for i in range(n)],
            "cityCode": rng.choice(CITIES, size=n, p=city_w),
            "cardTier": rng.choice(CARD_TIERS, size=n, p=[0.45, 0.32, 0.18, 0.05]),
            "syntheticSegment": rng.choice(
                ["MASS", "AFFLUENT", "YOUNG", "FAMILY"], size=n, p=[0.45, 0.2, 0.2, 0.15]
            ),
            "preferredCategories": ["|".join(p) for p in prefs],
            "spendingBand": rng.choice(["LOW", "MID", "HIGH"], size=n, p=[0.5, 0.35, 0.15]),
            "activityPattern": rng.choice(["DAY", "EVENING", "WEEKEND"], size=n, p=[.4, .4, .2]),
            "personalizationAllowed": rng.random(n) >= cfg.noPersonalizationShare,
            "isNewCustomer": rng.random(n) < cfg.newCustomerShare,
            "driftsPreference": rng.random(n) < cfg.driftCustomerShare,
        }
    )


def _merchants(rng: np.random.Generator, cfg: DatasetConfig) -> pd.DataFrame:
    n = cfg.merchantCount
    return pd.DataFrame(
        {
            "merchantId": [_ns(cfg, f"M{i:06d}") for i in range(n)],
            "merchantName": [f"Merchant {i:06d}" for i in range(n)],
            "categoryCode": rng.choice(CATEGORIES, size=n),
            "cityCode": rng.choice(CITIES, size=n, p=_zipf(rng, len(CITIES), 0.7)),
            "channel": rng.choice(["ONLINE", "OFFLINE"], size=n, p=[0.35, 0.65]),
            "rating": np.round(np.clip(rng.normal(4.1, 0.5, n), 1.0, 5.0), 2),
            "status": np.where(rng.random(n) < 0.05, "INACTIVE", "ACTIVE"),
            "popularity": _zipf(rng, n, 1.1),
        }
    )


def _promotions(rng, cfg: DatasetConfig, merchants: pd.DataFrame, ref: datetime) -> pd.DataFrame:
    n = cfg.promotionCount
    mids = rng.choice(merchants["merchantId"].to_numpy(), size=n, replace=False)
    start_offset = rng.integers(-30, 10, n)
    duration = rng.integers(7, 60, n)
    rows = []
    for i in range(n):
        starts = ref + timedelta(days=int(start_offset[i]))
        ends = starts + timedelta(days=int(duration[i]))
        expired = ends < ref
        tiers = sorted(
            rng.choice(CARD_TIERS, size=int(rng.integers(1, 5)), replace=False).tolist()
        )
        rows.append(
            {
                "promotionId": _ns(cfg, f"P{i:05d}"),
                "merchantId": mids[i],
                "benefitType": str(rng.choice(["CASHBACK", "DISCOUNT", "POINTS", "INSTALLMENT"])),
                "benefitValue": float(round(rng.uniform(5, 25), 1)),
                "minSpendMinor": int(rng.choice([0, 100_000, 250_000, 500_000])),
                "maxBenefitMinor": int(rng.choice([50_000, 100_000, 250_000])),
                "eligibleCardTiers": "|".join(tiers),
                "eligibleCityCodes": "|".join(
                    sorted(rng.choice(CITIES, size=int(rng.integers(1, 4)), replace=False).tolist())
                )
                if rng.random() < 0.6
                else "",
                "startsAt": starts,
                "endsAt": ends,
                "campaignQuota": int(rng.integers(50, 5000)),
                "quotaUsed": 0,
                "perCustomerLimit": int(rng.integers(1, 4)),
                "status": "EXPIRED" if expired else "ACTIVE",
            }
        )
    df = pd.DataFrame(rows)
    # SYN-005: a slice of promos is deliberately quota-exhausted
    exhausted = rng.random(n) < 0.08
    df.loc[exhausted, "quotaUsed"] = df.loc[exhausted, "campaignQuota"]
    return df


# ---------------------------------------------------------------- behaviour


def _transactions(rng, cfg, customers, merchants, promotions, ref):
    fail = cfg.effective_failures()
    n = cfg.transactionCount
    cust_ids = customers["customerId"].to_numpy()
    is_new = customers["isNewCustomer"].to_numpy()
    drifts = customers["driftsPreference"].to_numpy()
    prefs = [p.split("|") for p in customers["preferredCategories"]]
    cust_city = customers["cityCode"].to_numpy()
    band = customers["spendingBand"].to_numpy()

    m_ids = merchants["merchantId"].to_numpy()
    m_cat = merchants["categoryCode"].to_numpy()
    m_city = merchants["cityCode"].to_numpy()
    m_pop = merchants["popularity"].to_numpy()
    by_cat = {c: np.where(m_cat == c)[0] for c in CATEGORIES}

    # customer activity is itself long-tailed; new customers get far fewer events
    weights = _zipf(rng, len(cust_ids), 0.8)
    weights = np.where(is_new, weights * 0.1, weights)
    weights = weights / weights.sum()
    owners = rng.choice(len(cust_ids), size=n, p=weights)

    # event time: hour-of-day + weekday shape (SYN-003)
    day_off = rng.integers(0, cfg.historyDays, n)
    hour_p = np.array([.5, .3, .2, .2, .3, .8, 1.5, 2.5, 3, 3, 3.2, 4.5,
                       5, 4, 3.4, 3.4, 3.8, 4.6, 5, 4.4, 3.4, 2.4, 1.5, .8])
    hour_p = hour_p / hour_p.sum()
    hours = rng.choice(24, size=n, p=hour_p)
    minutes = rng.integers(0, 60, n)

    band_mult = {"LOW": 0.6, "MID": 1.0, "HIGH": 2.2}
    rows, purchases = [], []
    for k in range(n):
        ci = int(owners[k])
        cid = cust_ids[ci]
        pref = prefs[ci]
        # drifting customers swap preference in the recent third of history
        recent = day_off[k] < cfg.historyDays / 3
        if drifts[ci] and recent:
            pref = [c for c in CATEGORIES if c not in pref][:3] or pref
        cat = pref[int(rng.integers(0, len(pref)))] if rng.random() < 0.75 else str(
            rng.choice(CATEGORIES)
        )
        pool = by_cat[cat]
        if len(pool) == 0:
            pool = np.arange(len(m_ids))
        local = pool[m_city[pool] == cust_city[ci]]
        pool = local if (len(local) and rng.random() < 0.7) else pool
        p = m_pop[pool] / m_pop[pool].sum()
        mi = int(rng.choice(pool, p=p))

        occurred = ref - timedelta(days=int(day_off[k]))
        occurred = occurred.replace(hour=int(hours[k]), minute=int(minutes[k]), second=0,
                                    microsecond=0)
        ticket = CATEGORY_TICKET_MINOR[cat] * band_mult[band[ci]]
        amount = int(max(10_000, rng.lognormal(math.log(ticket), 0.55) // 1000 * 1000))
        tid = _uuid("txn", _salt(cfg), k)
        rows.append(
            {
                "eventId": _uuid("evt", _salt(cfg), k),
                "transactionId": tid,
                "customerId": cid,
                "merchantId": m_ids[mi],
                "transactionType": "PURCHASE",
                "amountMinor": amount,
                "currency": cfg.currency,
                "occurredAt": occurred,
                "originalTransactionId": None,
                "categoryCode": cat,
                "cityCode": m_city[mi],
                "injectedFault": "",
            }
        )
        purchases.append((k, tid, cid, m_ids[mi], amount, occurred, cat, m_city[mi]))

    rows += _corrections(rng, cfg, fail, purchases)
    rows += _faults(rng, cfg, fail, purchases)

    df = pd.DataFrame(rows)
    df = df.sort_values(["occurredAt", "eventId"], kind="mergesort").reset_index(drop=True)
    df["sourceSequence"] = np.arange(len(df))
    feedback = _feedback(rng, cfg, purchases, merchants, promotions)
    return df, feedback


def _corrections(rng, cfg, fail, purchases) -> list[dict]:
    rows = []
    for k, tid, cid, mid, amount, occurred, cat, city in purchases:
        r = rng.random()
        if r < cfg.reversalRate:
            typ, amt = "REVERSAL", amount
        elif r < cfg.reversalRate + cfg.refundRate:
            typ, amt = "REFUND", int(max(1000, amount * rng.uniform(0.1, 0.9) // 1000 * 1000))
        else:
            continue
        rows.append(
            {
                "eventId": _uuid("corr-evt", _salt(cfg), k),
                "transactionId": _uuid("corr-txn", _salt(cfg), k),
                "customerId": cid,
                "merchantId": mid,
                "transactionType": typ,
                "amountMinor": amt,
                "currency": cfg.currency,
                "occurredAt": occurred + timedelta(days=int(rng.integers(1, 10))),
                "originalTransactionId": tid,
                "categoryCode": cat,
                "cityCode": city,
                "injectedFault": "",
            }
        )
    return rows


def _faults(rng, cfg, fail, purchases) -> list[dict]:
    """SYN-005 — deliberate bad events, each tagged so tests can assert the outcome."""
    rows: list[dict] = []

    def pick(rate: float):
        return [p for p in purchases if rng.random() < rate]

    for k, tid, cid, mid, amount, occurred, cat, city in pick(fail.duplicateEventRate):
        rows.append(_fault_row(cfg, k, "DUPLICATE_EVENT", _uuid("evt", _salt(cfg), k), tid,
                               cid, mid, amount, occurred, cat, city))
    for k, tid, cid, mid, amount, occurred, cat, city in pick(fail.duplicateTransactionRate):
        # +1s so the redelivery sorts after the original; otherwise the eventId
        # tie-break can make the duplicate win and the original get rejected.
        rows.append(_fault_row(cfg, k, "DUPLICATE_TRANSACTION", _uuid("dup-evt", _salt(cfg), k),
                               tid, cid, mid, amount, occurred + timedelta(seconds=1), cat, city))
    for k, tid, cid, mid, amount, occurred, cat, city in pick(fail.zeroAmountRate):
        rows.append(_fault_row(cfg, k, "ZERO_AMOUNT", _uuid("zero-evt", _salt(cfg), k),
                               _uuid("zero-txn", _salt(cfg), k), cid, mid, 0, occurred, cat, city))
    for k, tid, cid, mid, amount, occurred, cat, city in pick(fail.excessiveRefundRate):
        row = _fault_row(cfg, k, "EXCESSIVE_REFUND", _uuid("over-evt", _salt(cfg), k),
                         _uuid("over-txn", _salt(cfg), k), cid, mid, amount * 3,
                         occurred + timedelta(seconds=1), cat, city)
        row.update(transactionType="REFUND", originalTransactionId=tid)
        rows.append(row)
    for k, tid, cid, mid, amount, occurred, cat, city in pick(fail.unknownCustomerRate):
        rows.append(_fault_row(cfg, k, "UNKNOWN_CUSTOMER", _uuid("uc-evt", _salt(cfg), k),
                               _uuid("uc-txn", _salt(cfg), k), _ns(cfg, "C9999999"), mid, amount,
                               occurred, cat, city))
    for k, tid, cid, mid, amount, occurred, cat, city in pick(fail.unknownMerchantRate):
        rows.append(_fault_row(cfg, k, "UNKNOWN_MERCHANT", _uuid("um-evt", _salt(cfg), k),
                               _uuid("um-txn", _salt(cfg), k), cid, _ns(cfg, "M999999"), amount,
                               occurred, cat, city))
    for k, tid, cid, mid, amount, occurred, cat, city in pick(fail.futureTimestampRate):
        rows.append(_fault_row(cfg, k, "FUTURE_TIMESTAMP", _uuid("fut-evt", _salt(cfg), k),
                               _uuid("fut-txn", _salt(cfg), k), cid, mid, amount,
                               cfg.referenceTime + timedelta(days=30), cat, city))
    return rows


def _fault_row(cfg, k, fault, eid, tid, cid, mid, amount, occurred, cat, city) -> dict:
    return {
        "eventId": eid,
        "transactionId": tid,
        "customerId": cid,
        "merchantId": mid,
        "transactionType": "PURCHASE",
        "amountMinor": int(amount),
        "currency": cfg.currency,
        "occurredAt": occurred,
        "originalTransactionId": None,
        "categoryCode": cat,
        "cityCode": city,
        "injectedFault": fault,
    }


def _feedback(rng, cfg, purchases, merchants, promotions) -> pd.DataFrame:
    """SYN-004 — outcomes from latent preference + promo sensitivity + position bias.

    Deliberately NOT the baseline ranking formula, so evaluation is not circular.
    """
    promo_merchants = set(
        promotions.loc[promotions["status"] == "ACTIVE", "merchantId"].tolist()
    )
    m_rating = dict(zip(merchants["merchantId"], merchants["rating"]))
    rows = []
    sample = purchases[:: max(1, len(purchases) // 5000)]
    for k, tid, cid, mid, amount, occurred, cat, city in sample:
        request_id = _uuid("req", _salt(cfg), k)
        shown = [mid] + [
            str(x) for x in rng.choice(merchants["merchantId"].to_numpy(), size=7, replace=False)
        ]
        for pos, m in enumerate(shown):
            latent = float(rng.beta(2, 5))
            promo_lift = 0.25 if m in promo_merchants else 0.0
            rating_lift = (m_rating.get(m, 4.0) - 4.0) * 0.1
            position_bias = 1.0 / math.log2(pos + 2)
            p_click = min(0.95, max(0.0, (latent + promo_lift + rating_lift) * position_bias))
            impression_id = _uuid("imp", _salt(cfg), k, pos)
            rows.append({
                "requestId": request_id, "impressionId": impression_id, "customerId": cid,
                "merchantId": m, "position": pos, "eventType": "IMPRESSION",
                "occurredAt": occurred, "label": 0,
            })
            if rng.random() >= p_click:
                continue
            rows.append({
                "requestId": request_id, "impressionId": impression_id, "customerId": cid,
                "merchantId": m, "position": pos, "eventType": "CLICK",
                "occurredAt": occurred + timedelta(seconds=30), "label": 1,
            })
            if m in promo_merchants and rng.random() < 0.35:
                rows.append({
                    "requestId": request_id, "impressionId": impression_id, "customerId": cid,
                    "merchantId": m, "position": pos, "eventType": "PROMO_ACTIVATION",
                    "occurredAt": occurred + timedelta(minutes=2), "label": 2,
                })
                if rng.random() < 0.4:
                    rows.append({
                        "requestId": request_id, "impressionId": impression_id, "customerId": cid,
                        "merchantId": m, "position": pos, "eventType": "REDEMPTION",
                        "occurredAt": occurred + timedelta(hours=1), "label": 3,
                    })
    return pd.DataFrame(rows).sort_values(
        ["occurredAt", "impressionId", "eventType"], kind="mergesort"
    ).reset_index(drop=True)


# ---------------------------------------------------------------- output


def _write(tables: dict[str, pd.DataFrame], cfg: DatasetConfig, out_dir: Path) -> dict:
    files = {}
    for name, df in tables.items():
        for fmt in cfg.outputFormats:
            path = out_dir / f"{name}.{'parquet' if fmt == 'parquet' else 'jsonl'}"
            if fmt == "parquet":
                df.to_parquet(path, index=False, compression="snappy")
            else:
                path.write_text(df.to_json(orient="records", lines=True, date_format="iso"))
            files[path.name] = {
                "rows": len(df),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
    # SIM-001 replay file: envelope-shaped, event-time ordered
    replay = out_dir / "replay.jsonl"
    with replay.open("w") as fh:
        for row in tables["transactions"].to_dict(orient="records"):
            fh.write(json.dumps(_envelope(row), default=str, sort_keys=True) + "\n")
    files[replay.name] = {
        "rows": len(tables["transactions"]),
        "bytes": replay.stat().st_size,
        "sha256": _sha256(replay),
    }
    return files


def _nz(value):
    """pandas turns absent values into NaN; JSON has no NaN."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _envelope(row: dict) -> dict:
    row = {k: _nz(v) for k, v in row.items()}
    return {
        "eventId": row["eventId"],
        "eventType": "transaction.created",
        "eventVersion": "1.0.0",
        "occurredAt": row["occurredAt"],
        "producer": "synthetic-generator",
        "correlationId": row["transactionId"],
        "injectedFault": row.get("injectedFault") or None,
        "payload": {
            "transactionId": row["transactionId"],
            "customerId": row["customerId"],
            "merchantId": row["merchantId"],
            "transactionType": row["transactionType"],
            "amountMinor": int(row["amountMinor"]),
            "currency": row["currency"],
            "occurredAt": row["occurredAt"],
            "originalTransactionId": row["originalTransactionId"],
            "categoryCode": row["categoryCode"],
            "cityCode": row["cityCode"],
        },
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _quality_report(tables: dict[str, pd.DataFrame], cfg: DatasetConfig) -> dict:
    txn, cust, merch = tables["transactions"], tables["customers"], tables["merchants"]
    known_c, known_m = set(cust["customerId"]), set(merch["merchantId"])
    faults = txn["injectedFault"].replace("", np.nan).dropna()
    return {
        "transactionRows": len(txn),
        "duplicateEventIds": int(txn["eventId"].duplicated().sum()),
        "duplicateTransactionIds": int(txn["transactionId"].duplicated().sum()),
        "nonPositiveAmounts": int((txn["amountMinor"] <= 0).sum()),
        "unknownCustomerRefs": int((~txn["customerId"].isin(known_c)).sum()),
        "unknownMerchantRefs": int((~txn["merchantId"].isin(known_m)).sum()),
        "futureTimestamps": int((txn["occurredAt"] > cfg.referenceTime).sum()),
        "orphanCorrections": int(
            (~txn["originalTransactionId"].dropna().isin(set(txn["transactionId"]))).sum()
        ),
        "injectedFaultCounts": {str(k): int(v) for k, v in faults.value_counts().items()},
    }


def _distribution_summary(tables: dict[str, pd.DataFrame]) -> dict:
    txn = tables["transactions"]
    clean = txn[txn["injectedFault"] == ""]
    return {
        "byCategory": {
            str(k): int(v) for k, v in clean["categoryCode"].value_counts().sort_index().items()
        },
        "byTransactionType": {
            str(k): int(v) for k, v in clean["transactionType"].value_counts().sort_index().items()
        },
        "byHourUtc": {
            str(k): int(v)
            for k, v in clean["occurredAt"].dt.hour.value_counts().sort_index().items()
        },
        "amountMinorPercentiles": {
            str(p): int(clean["amountMinor"].quantile(p / 100)) for p in (50, 90, 99)
        },
        "merchantCoverage": int(clean["merchantId"].nunique()),
        "customerCoverage": int(clean["customerId"].nunique()),
    }
