"""ML-005 steps 1-7 — build a leakage-free Learning-to-Rank dataset from a dataset dir.

The point-in-time join is structural, not a filter bolted on afterwards: transactions
and recommendation requests are merged into one event-time ordered timeline, and a
request can only ever see the ledger state built from events that preceded it. There is
no code path by which a future transaction can reach a past training row.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from rec.core.eligibility import evaluate, offer_view
from rec.core.features import compute_features
from rec.core.ledger import CustomerState, Reject, apply_event
from rec.core.models import CardTier, Channel, Customer, Envelope, Merchant, Promotion
from rec.ml.attribution import (
    ATTRIBUTION_WINDOW,
    OBSERVATION_WINDOW,
    label_from_events,
    policy_metadata,
)
from rec.ml.vectorize import FEATURE_NAMES, vectorize

DATASET_BUILDER_VERSION = "1.0.0"
GROUP_COLUMN = "requestId"
LABEL_COLUMN = "label"
SEGMENT_COLUMNS = ("segment_history", "segment_city", "segment_card_tier")


@dataclass
class Split:
    train: pd.DataFrame
    test: pd.DataFrame
    gap_days: int
    boundary: datetime


def build(
    dataset_dir: Path,
    *,
    as_of: datetime | None = None,
    observation_window: timedelta = OBSERVATION_WINDOW,
    attribution_window: timedelta = ATTRIBUTION_WINDOW,
    exclude_customers: frozenset[str] = frozenset(),
) -> tuple[pd.DataFrame, dict]:
    """Returns (rows, metadata). One row per exposed candidate, grouped by requestId.

    `exclude_customers` are erased customers (AC-009): the files on disk predate the
    erasure, so they are filtered on every read rather than trusted."""
    customers = {c["customerId"]: c for c in _read(dataset_dir, "customers")
                 if c["customerId"] not in exclude_customers}
    merchants = {m["merchantId"]: _merchant(m) for m in _read(dataset_dir, "merchants")}
    promotions = [_promotion(p) for p in _read(dataset_dir, "promotions")]
    promos_by_merchant: dict[str, list[Promotion]] = {}
    for promo in promotions:
        promos_by_merchant.setdefault(promo.merchantId, []).append(promo)

    feedback = [e for e in _read(dataset_dir, "feedback_events")
                if e["customerId"] not in exclude_customers]
    as_of = as_of or max(_ts(e["occurredAt"]) for e in feedback) + observation_window
    labels = label_from_events(feedback, as_of=as_of, observation_window=observation_window,
                              attribution_window=attribution_window)

    requests = _requests(feedback, labels)
    events = _events(dataset_dir, set(customers))
    rows = _walk(events, requests, customers, merchants, promos_by_merchant)

    frame = pd.DataFrame(rows)
    metadata = {
        "datasetBuilderVersion": DATASET_BUILDER_VERSION,
        "datasetDir": dataset_dir.name,
        "asOf": as_of.isoformat(),
        "featureNames": list(FEATURE_NAMES),
        "rows": len(frame),
        "requestGroups": int(frame[GROUP_COLUMN].nunique()) if len(frame) else 0,
        "labelDistribution": (frame[LABEL_COLUMN].value_counts().sort_index().to_dict()
                              if len(frame) else {}),
        "withheldUnobservableImpressions":
            sum(1 for e in feedback if e["eventType"] == "IMPRESSION") - len(labels),
        "attribution": policy_metadata(),
        # quotaUsed is current state, not history: promo quota exhaustion cannot be
        # reconstructed point-in-time from a static master table.
        "excludedErasedCustomers": len(exclude_customers),
        "knownLimitations": ["promo quota evaluated from current quota_used, not as-of"],
    }
    return frame, metadata


# ---------------------------------------------------------------- timeline


@dataclass
class _Request:
    requestId: str
    customerId: str
    requestTime: datetime
    items: list[tuple[str, int, int]]  # (merchantId, position, label)


def _requests(feedback: list[dict], labels: dict[str, int]) -> list[_Request]:
    grouped: dict[str, _Request] = {}
    for event in feedback:
        if event["eventType"] != "IMPRESSION":
            continue
        label = labels.get(event["impressionId"])
        if label is None:
            continue  # observation window still open (ML-003)
        shown = _ts(event["occurredAt"])
        request = grouped.get(event["requestId"])
        if request is None:
            request = _Request(event["requestId"], event["customerId"], shown, [])
            grouped[event["requestId"]] = request
        request.requestTime = min(request.requestTime, shown)
        request.items.append((event["merchantId"], int(event.get("position", 0)), label))
    return sorted(grouped.values(), key=lambda r: (r.requestTime, r.requestId))


def _events(dataset_dir: Path, known_customers: set[str]) -> list[tuple[datetime, Envelope]]:
    out: list[tuple[datetime, Envelope]] = []
    with (dataset_dir / "replay.jsonl").open() as fh:
        for line in fh:
            raw = json.loads(line)
            if raw.get("payload", {}).get("customerId") not in known_customers:
                continue
            try:
                envelope = Envelope.model_validate(raw)
            except Exception:
                continue  # invalid events never reached the feature store either
            out.append((envelope.occurredAt.astimezone(UTC), envelope))
    out.sort(key=lambda pair: (pair[0], pair[1].eventId))
    return out


def _walk(
    events: list[tuple[datetime, Envelope]],
    requests: list[_Request],
    customers: dict[str, dict],
    merchants: dict[str, Merchant],
    promos_by_merchant: dict[str, list[Promotion]],
) -> list[dict]:
    states: dict[str, CustomerState] = {}
    seen: set[str] = set()
    merchant_city = {m.merchantId: m.cityCode for m in merchants.values()}
    rows: list[dict] = []
    cursor = 0

    for request in requests:
        # Advance the ledger to — and never past — this request's timestamp.
        while cursor < len(events) and events[cursor][0] <= request.requestTime:
            _, envelope = events[cursor]
            cursor += 1
            if envelope.eventId in seen:
                continue
            seen.add(envelope.eventId)
            customer_id = envelope.payload["customerId"]
            state = states.setdefault(customer_id, CustomerState(customer_id))
            try:
                apply_event(state, envelope, now=request.requestTime)
            except (Reject, AssertionError, ValueError):
                continue

        record = customers.get(request.customerId)
        if record is None:
            continue
        state = states.get(request.customerId) or CustomerState(request.customerId)
        features = compute_features(state, request.requestTime, merchant_city=merchant_city)
        if not record.get("personalizationAllowed", True):
            features = features | {"categoryInterest": {}, "merchantAffinity": {},
                                   "personalizationAllowed": False}
        customer = Customer(
            customerId=request.customerId, cityCode=record["cityCode"],
            cardTier=CardTier(record["cardTier"]),
            personalizationAllowed=bool(record.get("personalizationAllowed", True)))

        for merchant_id, position, label in request.items:
            merchant = merchants.get(merchant_id)
            if merchant is None:
                continue
            promo = _eligible_promo(promos_by_merchant.get(merchant_id, []), customer, merchant,
                                    request.requestTime)
            vector = vectorize(
                features, merchant_id=merchant_id, merchant_rating=merchant.rating,
                merchant_channel=merchant.channel.value, merchant_category=merchant.categoryCode,
                merchant_city=merchant.cityCode, city_code=customer.cityCode,
                promo=promo, request_time=request.requestTime)
            rows.append({
                GROUP_COLUMN: request.requestId,
                "customerId": request.customerId,
                "merchantId": merchant_id,
                "categoryCode": merchant.categoryCode,
                "requestTime": request.requestTime,
                "position": position,
                LABEL_COLUMN: label,
                "segment_history": _history_bucket(features.get("historyDepthDays") or 0,
                                                   features.get("transactionCount90d") or 0),
                "segment_city": customer.cityCode,
                "segment_card_tier": customer.cardTier.value,
                **vector,
            })
    return rows


def _eligible_promo(promos: list[Promotion], customer: Customer, merchant: Merchant,
                    at: datetime) -> dict | None:
    for promo in promos:
        ok, _ = evaluate(promo, customer, merchant, now=at)
        if ok:
            return offer_view(promo) | {"endsAt": promo.endsAt, "benefitValue": promo.benefitValue}
    return None


def _history_bucket(depth_days: float, txn_count: int) -> str:
    if txn_count < 3:
        return "COLD"
    if depth_days < 30:
        return "NEW"
    if txn_count < 20:
        return "LIGHT"
    return "HEAVY"


# ---------------------------------------------------------------- split


def temporal_split(frame: pd.DataFrame, *, test_fraction: float = 0.25,
                   gap: timedelta = OBSERVATION_WINDOW) -> Split:
    """ML-005 steps 6-7: split by request time and drop a gap the width of the
    observation window, so a training request's outcomes cannot overlap the test period."""
    if frame.empty:
        return Split(frame, frame, gap.days, datetime.now(UTC))
    groups = (frame[[GROUP_COLUMN, "requestTime"]].drop_duplicates()
              .sort_values("requestTime", kind="mergesort"))
    boundary_index = max(int(len(groups) * (1 - test_fraction)), 1)
    boundary = groups.iloc[boundary_index - 1]["requestTime"]
    boundary = pd.Timestamp(boundary).to_pydatetime()
    train = frame[frame["requestTime"] <= boundary]
    test = frame[frame["requestTime"] > boundary + gap]
    return Split(train.reset_index(drop=True), test.reset_index(drop=True), gap.days, boundary)


# ---------------------------------------------------------------- io


def _read(dataset_dir: Path, name: str) -> list[dict]:
    parquet = dataset_dir / f"{name}.parquet"
    if parquet.exists():
        return pd.read_parquet(parquet).to_dict(orient="records")
    lines = (dataset_dir / f"{name}.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _merchant(row: dict) -> Merchant:
    return Merchant(
        merchantId=row["merchantId"], merchantName=row["merchantName"],
        categoryCode=row["categoryCode"], cityCode=row["cityCode"],
        channel=Channel(row["channel"]), rating=float(row["rating"]), status=row["status"])


def _promotion(row: dict) -> Promotion:
    return Promotion(
        promotionId=row["promotionId"], merchantId=row["merchantId"],
        benefitType=row["benefitType"], benefitValue=float(row["benefitValue"]),
        minSpendMinor=int(row["minSpendMinor"]), maxBenefitMinor=int(row["maxBenefitMinor"]),
        eligibleCardTiers=[CardTier(t) for t in str(row["eligibleCardTiers"]).split("|") if t],
        eligibleCityCodes=[c for c in str(row["eligibleCityCodes"]).split("|") if c],
        startsAt=_ts(row["startsAt"]), endsAt=_ts(row["endsAt"]),
        campaignQuota=int(row["campaignQuota"]), quotaUsed=int(row["quotaUsed"]),
        perCustomerLimit=int(row["perCustomerLimit"]), status=row["status"])


def _ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, pd.Timestamp):
        return value.tz_localize(UTC).to_pydatetime() if value.tz is None \
            else value.to_pydatetime()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
