"""Transaction lifecycle + rolling feature state (SDD 6.2, FEAT-001..003).

One implementation, two callers: the Kafka consumer feeds it live, the offline
oracle feeds it a replayed dataset. AC-008 reconciliation compares the two.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .models import Envelope, TxnType

MAX_WINDOW_DAYS = 90
# Corrections arrive long after the purchase leaves the feature window, so the
# transaction ledger is retained longer than the buckets it feeds.
TXN_RETENTION_DAYS = 180
LATE_ARRIVAL_LIMIT_HOURS = 24  # FEAT-003, configurable
FUTURE_SKEW_LIMIT_MINUTES = 5  # SYN-005: too-far-future is quarantined


class Reject(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass
class TxnRecord:
    day: str
    categoryCode: str
    merchantId: str
    amountMinor: int
    refundedMinor: int = 0
    reversed: bool = False

    @property
    def remainingMinor(self) -> int:
        return 0 if self.reversed else self.amountMinor - self.refundedMinor


@dataclass
class CustomerState:
    """Daily buckets keyed (day, category, merchant) -> [effective_count, net_minor].

    Window expiration is computed as-of read time, so a customer with no new
    events still ages out correctly (AC-003) without any scheduler.
    """

    customerId: str
    buckets: dict[tuple[str, str, str], list[int]] = field(default_factory=dict)
    txns: dict[str, TxnRecord] = field(default_factory=dict)
    seenEvents: set[str] = field(default_factory=set)
    hours: dict[int, int] = field(default_factory=dict)
    lastEventOccurredAt: datetime | None = None
    featureVersion: int = 0

    # Write amplification control: the online store persists only what changed.
    # Rewriting a heavy customer's whole state on every event does not scale.
    dirtyBuckets: set[tuple[str, str, str]] = field(default_factory=set)
    dirtyTxns: set[str] = field(default_factory=set)
    dirtyHours: set[int] = field(default_factory=set)
    removedBuckets: set[tuple[str, str, str]] = field(default_factory=set)
    removedTxns: set[str] = field(default_factory=set)

    def _bucket(self, day: str, cat: str, merch: str) -> list[int]:
        self.touch_bucket((day, cat, merch))
        return self.buckets.setdefault((day, cat, merch), [0, 0])

    def touch_bucket(self, key: tuple[str, str, str]) -> None:
        self.dirtyBuckets.add(key)
        self.removedBuckets.discard(key)

    def touch_txn(self, txn_id: str) -> None:
        self.dirtyTxns.add(txn_id)
        self.removedTxns.discard(txn_id)

    def clear_dirty(self) -> None:
        self.dirtyBuckets.clear()
        self.dirtyTxns.clear()
        self.dirtyHours.clear()
        self.removedBuckets.clear()
        self.removedTxns.clear()


def _day(dt: datetime) -> str:
    return dt.astimezone(UTC).date().isoformat()


def apply_event(state: CustomerState, env: Envelope, *, now: datetime | None = None) -> str:
    """Mutate state. Returns an outcome code. Raises Reject for quarantine.

    Idempotent on eventId AND on business transaction identity (EVT-003).
    """
    now = now or datetime.now(UTC)
    if env.eventId in state.seenEvents:
        return "DUPLICATE_EVENT"
    txn = env.transaction()  # raises on amount<=0 / schema mismatch

    occurred = txn.occurredAt.astimezone(UTC)
    if occurred > now + timedelta(minutes=FUTURE_SKEW_LIMIT_MINUTES):
        raise Reject("FUTURE_TIMESTAMP", occurred.isoformat())

    if txn.transactionType is TxnType.PURCHASE:
        outcome = _apply_purchase(state, txn)
    else:
        outcome = _apply_correction(state, txn)

    state.seenEvents.add(env.eventId)
    if state.lastEventOccurredAt is None or occurred > state.lastEventOccurredAt:
        state.lastEventOccurredAt = occurred
    state.featureVersion += 1
    return outcome


def _apply_purchase(state: CustomerState, txn) -> str:
    if txn.transactionId in state.txns:
        return "DUPLICATE_TRANSACTION"
    if not txn.categoryCode:
        raise Reject("MISSING_CATEGORY", txn.merchantId)
    day = _day(txn.occurredAt)
    rec = TxnRecord(day, txn.categoryCode, txn.merchantId, txn.amountMinor)
    state.txns[txn.transactionId] = rec
    state.touch_txn(txn.transactionId)
    hour = txn.occurredAt.astimezone(UTC).hour
    state.hours[hour] = state.hours.get(hour, 0) + 1
    state.dirtyHours.add(hour)
    b = state._bucket(day, rec.categoryCode, rec.merchantId)
    b[0] += 1
    b[1] += txn.amountMinor
    return "APPLIED"


def _apply_correction(state: CustomerState, txn) -> str:
    """Refund/reversal: category + day follow the ORIGINAL transaction (SDD 6.2.7)."""
    orig_id = txn.originalTransactionId
    if not orig_id:
        raise Reject("MISSING_ORIGINAL", txn.transactionId)
    orig = state.txns.get(orig_id)
    if orig is None:
        raise Reject("UNKNOWN_ORIGINAL", orig_id)
    if txn.transactionId in state.txns:
        return "DUPLICATE_TRANSACTION"
    if orig.reversed:
        raise Reject("ALREADY_REVERSED", orig_id)
    if txn.amountMinor > orig.remainingMinor:
        raise Reject("REFUND_EXCEEDS_ORIGINAL", f"{txn.amountMinor}>{orig.remainingMinor}")

    fully_cancelled = (
        txn.transactionType is TxnType.REVERSAL or txn.amountMinor == orig.remainingMinor
    )
    # The original may already have aged out of the feature window; then there is
    # nothing left to subtract and creating a bucket would drive net negative.
    bucket_key = (orig.day, orig.categoryCode, orig.merchantId)
    b = state.buckets.get(bucket_key)
    if b is not None:
        state.touch_bucket(bucket_key)
        b[1] -= txn.amountMinor
        if fully_cancelled:
            b[0] -= 1
        assert b[1] >= 0, "net monetary must not go negative (SDD 6.2.4)"
    if fully_cancelled:
        orig.reversed = True  # SDD 6.2.5: full cancellation drops out of frequency
    orig.refundedMinor += txn.amountMinor
    state.touch_txn(orig_id)
    # marker so a replayed correction is not applied twice
    state.txns[txn.transactionId] = TxnRecord(orig.day, orig.categoryCode, orig.merchantId, 0)
    state.touch_txn(txn.transactionId)
    return "APPLIED"


def prune(state: CustomerState, as_of: datetime) -> None:
    """Drop aged-out buckets and ledger entries. Safe to call anytime."""
    today = as_of.astimezone(UTC).date()
    bucket_cutoff = (today - timedelta(days=MAX_WINDOW_DAYS)).isoformat()
    txn_cutoff = (today - timedelta(days=TXN_RETENTION_DAYS)).isoformat()
    for key in [k for k in state.buckets if k[0] < bucket_cutoff]:
        del state.buckets[key]
        state.dirtyBuckets.discard(key)
        state.removedBuckets.add(key)
    for txn_id in [t for t, r in state.txns.items() if r.day < txn_cutoff]:
        del state.txns[txn_id]
        state.dirtyTxns.discard(txn_id)
        state.removedTxns.add(txn_id)


def in_window(state: CustomerState, as_of: datetime, days: int):
    """Yield ((day, category, merchant), [count, net]) inside the window."""
    end = as_of.astimezone(UTC).date()
    start = (end - timedelta(days=days)).isoformat()
    end_s = end.isoformat()
    for key, val in state.buckets.items():
        if start < key[0] <= end_s and (val[0] > 0 or val[1] != 0):
            yield key, val


def last_day_in_window(state: CustomerState, as_of: datetime, predicate) -> date | None:
    days = [date.fromisoformat(k[0]) for k, _ in in_window(state, as_of, MAX_WINDOW_DAYS)
            if predicate(k)]
    return max(days) if days else None
