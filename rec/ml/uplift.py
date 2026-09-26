"""Promo uplift (ADR-0010): a randomised holdout, then CausalML meta-learners.

Serving side (cheap, no CausalML import): `PROMO_HOLDOUT_PERCENT` of customers, chosen by a
salted hash like the canary, are served recommendations without promotion offers, and the
first arm each customer saw is recorded in `promo_experiment`.

Offline side (`python -m rec.ml.uplift`, needs `.[causal]`): for every customer whose
outcome window has closed, features as of first exposure, treatment = promo offers shown,
outcome = a purchase at a promo merchant within the window. An X-learner estimates the
per-customer effect (CATE); Qini on a held-out set of customers says whether its ordering
beats random. Nothing here changes who gets a promo: acting on CATE is a later decision.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from rec.store import pg

log = logging.getLogger("uplift")
HOLDOUT_SALT = "promo-holdout:"  # independent of the canary split
OUTCOME_WINDOW = timedelta(days=14)
FEATURES = ("txn_count_90d", "log_spend_90d", "days_since_last_txn", "history_days",
            "promo_purchase_share", "card_tier_rank")
CARD_TIER_RANK = {"CLASSIC": 0, "GOLD": 1, "PLATINUM": 2, "INFINITE": 3}


def in_holdout(customer_id: str, percent: int) -> bool:
    """Deterministic per customer, so a customer never flips arm between requests."""
    if percent <= 0:
        return False
    digest = hashlib.sha256((HOLDOUT_SALT + customer_id).encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100 < percent


async def record_exposure(customer_id: str, holdout: bool, percent: int) -> None:
    """First arm only. Fire-and-forget from serving; a lost row costs one sample."""
    try:
        conn = await pg.pool()
        await conn.execute(
            """INSERT INTO promo_experiment (customer_id, arm, holdout_percent)
               VALUES ($1, $2, $3) ON CONFLICT (customer_id) DO NOTHING""",
            customer_id, "HOLDOUT" if holdout else "TREATMENT", percent)
    except Exception as exc:  # noqa: BLE001 - never affects the served response
        log.info("promo exposure not recorded: %s", exc)


# ---------------------------------------------------------------------- offline


async def load_frame(now: datetime | None = None) -> pd.DataFrame:
    """One row per exposed customer whose outcome window has closed."""
    now = now or datetime.now(UTC)
    conn = await pg.pool()
    rows = await conn.fetch(
        """
        WITH promo_merchants AS (SELECT DISTINCT merchant_id FROM promotions),
        purchases AS (
          SELECT t.customer_id, t.occurred_at, t.amount_minor,
                 (pm.merchant_id IS NOT NULL) AS at_promo_merchant
          FROM transaction_log t LEFT JOIN promo_merchants pm USING (merchant_id)
          WHERE t.outcome = 'APPLIED' AND t.txn_type = 'PURCHASE')
        SELECT e.customer_id, e.arm, e.holdout_percent, e.first_exposed_at, c.card_tier,
          count(p.*) FILTER (WHERE p.occurred_at <= e.first_exposed_at
                               AND p.occurred_at > e.first_exposed_at - interval '90 days')
            AS txn_count_90d,
          coalesce(sum(p.amount_minor) FILTER (
            WHERE p.occurred_at <= e.first_exposed_at
              AND p.occurred_at > e.first_exposed_at - interval '90 days'), 0) AS spend_90d,
          max(p.occurred_at) FILTER (WHERE p.occurred_at <= e.first_exposed_at) AS last_txn,
          min(p.occurred_at) FILTER (WHERE p.occurred_at <= e.first_exposed_at) AS first_txn,
          count(p.*) FILTER (WHERE p.occurred_at <= e.first_exposed_at) AS txn_before,
          count(p.*) FILTER (WHERE p.occurred_at <= e.first_exposed_at
                               AND p.at_promo_merchant) AS promo_txn_before,
          coalesce(bool_or(p.at_promo_merchant AND p.occurred_at > e.first_exposed_at
                           AND p.occurred_at <= e.first_exposed_at + $2::interval), false)
            AS converted
        FROM promo_experiment e
        JOIN customers c USING (customer_id)
        LEFT JOIN purchases p ON p.customer_id = e.customer_id
        WHERE e.first_exposed_at <= $1::timestamptz - $2::interval
        GROUP BY e.customer_id, e.arm, e.holdout_percent, e.first_exposed_at, c.card_tier
        """, now, OUTCOME_WINDOW)
    return features(pd.DataFrame([dict(r) for r in rows]))


def features(raw: pd.DataFrame) -> pd.DataFrame:
    """Pure: SQL aggregates -> model columns + treatment/outcome/propensity."""
    if raw.empty:
        return raw
    exposed = pd.to_datetime(raw["first_exposed_at"], utc=True)

    def days_before(column: str, missing: float) -> pd.Series:
        stamps = pd.to_datetime(raw[column], utc=True)
        return ((exposed - stamps).dt.total_seconds() / 86400).fillna(missing)

    return pd.DataFrame({
        "customer_id": raw["customer_id"],
        "txn_count_90d": raw["txn_count_90d"].astype(float),
        "log_spend_90d": raw["spend_90d"].astype(float).map(lambda v: math.log1p(max(v, 0))),
        "days_since_last_txn": days_before("last_txn", 365.0),
        "history_days": days_before("first_txn", 0.0),
        "promo_purchase_share": (raw["promo_txn_before"] / raw["txn_before"].clip(lower=1))
        .astype(float),
        "card_tier_rank": raw["card_tier"].map(CARD_TIER_RANK).fillna(0).astype(float),
        "treatment": (raw["arm"] == "TREATMENT").astype(int),
        # randomised by design: the propensity is the configured split, not an estimate
        "propensity": 1 - raw["holdout_percent"].astype(float) / 100,
        "converted": raw["converted"].astype(int),
    })


def fit(frame: pd.DataFrame, *, seed: int = 42, test_share: float = 0.3,
        min_effect: float = 0.01) -> dict:
    """X-learner CATE, graded by Qini on customers it was not fitted on."""
    from causalml.inference.meta import BaseXClassifier
    from causalml.metrics import qini_score
    from xgboost import XGBClassifier, XGBRegressor

    if frame["treatment"].nunique() < 2 or len(frame) < 200:
        raise ValueError("need both arms and at least 200 customers with a closed window")
    rng = np.random.default_rng(seed)
    held_out = rng.random(len(frame)) < test_share
    train, test = frame[~held_out], frame[held_out]
    model = BaseXClassifier(
        outcome_learner=XGBClassifier(n_estimators=200, max_depth=3, random_state=seed),
        effect_learner=XGBRegressor(n_estimators=200, max_depth=3, random_state=seed))
    model.fit(X=train[list(FEATURES)].to_numpy(), treatment=train["treatment"].to_numpy(),
              y=train["converted"].to_numpy(), p=train["propensity"].to_numpy())
    cate = model.predict(X=test[list(FEATURES)].to_numpy(),
                         p=test["propensity"].to_numpy()).ravel()
    graded = pd.DataFrame({"y": test["converted"].to_numpy(),
                           "w": test["treatment"].to_numpy(),
                           "cate": cate, "random": rng.random(len(test))})
    qini = qini_score(graded, outcome_col="y", treatment_col="w", normalize=True)
    arms = frame.groupby("treatment")["converted"].mean()
    return {
        "customers": int(len(frame)), "heldOut": int(held_out.sum()),
        "conversion": {"treatment": float(arms.get(1, float("nan"))),
                       "holdout": float(arms.get(0, float("nan")))},
        "averageEffect": float(arms.get(1, 0) - arms.get(0, 0)),
        "qini": {"model": float(qini["cate"]), "random": float(qini["random"])},
        # effect segments on held-out customers; "no effect" covers sure things and lost
        # causes alike, which only a control-outcome model could separate
        "segments": {"persuadable": float((cate > min_effect).mean()),
                     "noEffect": float((abs(cate) <= min_effect).mean()),
                     "sleepingDog": float((cate < -min_effect).mean())},
    }


async def save_report(report: dict) -> None:
    conn = await pg.pool()
    await conn.execute("INSERT INTO uplift_reports (report) VALUES ($1)", json.dumps(report))


async def latest_report() -> dict | None:
    conn = await pg.pool()
    row = await conn.fetchrow(
        "SELECT created_at, report FROM uplift_reports ORDER BY id DESC LIMIT 1")
    return {"createdAt": row["created_at"], **json.loads(row["report"])} if row else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate promo uplift from the holdout.")
    parser.add_argument("--now", type=datetime.fromisoformat, default=None)
    args = parser.parse_args()

    async def run() -> dict:
        try:
            report = fit(await load_frame(args.now))
            await save_report(report)  # the dashboard shows the latest one
            return report
        finally:
            await pg.close()

    print(json.dumps(asyncio.run(run()), indent=2))


if __name__ == "__main__":
    main()
