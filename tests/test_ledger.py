"""AC-001 dedup, AC-002 refund, AC-003 window expiration."""
from datetime import UTC, datetime, timedelta

import pytest

from rec.core.features import compute_features
from rec.core.ledger import CustomerState, Reject, apply_event
from rec.core.models import Envelope

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def ev(eid, tid, typ="PURCHASE", amount=150_000, *, at=NOW, orig=None, cat="F&B", merch="M1"):
    return Envelope(
        eventId=eid,
        eventType="transaction.created",
        occurredAt=at,
        payload={
            "transactionId": tid,
            "customerId": "C1",
            "merchantId": merch,
            "transactionType": typ,
            "amountMinor": amount,
            "occurredAt": at.isoformat(),
            "originalTransactionId": orig,
            "categoryCode": cat,
        },
    )


def feats(state, as_of=NOW):
    return compute_features(state, as_of)


def test_ac001_duplicate_event_and_transaction_are_noops():
    s = CustomerState("C1")
    apply_event(s, ev("e1", "t1"), now=NOW)
    before = feats(s)
    assert apply_event(s, ev("e1", "t1"), now=NOW) == "DUPLICATE_EVENT"
    assert apply_event(s, ev("e2", "t1"), now=NOW) == "DUPLICATE_TRANSACTION"
    after = feats(s)
    assert before["transactionCount90d"] == after["transactionCount90d"] == 1
    assert before["netSpend90d"] == after["netSpend90d"] == 150_000


def test_ac002_partial_refund_cuts_monetary_not_frequency():
    s = CustomerState("C1")
    apply_event(s, ev("e1", "t1", amount=150_000), now=NOW)
    apply_event(s, ev("e2", "r1", "REFUND", 50_000, orig="t1"), now=NOW)
    f = feats(s)
    assert f["netSpend90d"] == 100_000
    assert f["transactionCount90d"] == 1


def test_full_reversal_removes_frequency():
    s = CustomerState("C1")
    apply_event(s, ev("e1", "t1", amount=150_000), now=NOW)
    apply_event(s, ev("e2", "r1", "REVERSAL", 150_000, orig="t1"), now=NOW)
    f = feats(s)
    assert f["netSpend90d"] == 0
    assert f["transactionCount90d"] == 0


def test_refund_exceeding_original_is_quarantined():
    s = CustomerState("C1")
    apply_event(s, ev("e1", "t1", amount=150_000), now=NOW)
    apply_event(s, ev("e2", "r1", "REFUND", 100_000, orig="t1"), now=NOW)
    with pytest.raises(Reject, match="REFUND_EXCEEDS_ORIGINAL"):
        apply_event(s, ev("e3", "r2", "REFUND", 100_000, orig="t1"), now=NOW)


def test_reversal_then_refund_rejected():
    s = CustomerState("C1")
    apply_event(s, ev("e1", "t1"), now=NOW)
    apply_event(s, ev("e2", "r1", "REVERSAL", 150_000, orig="t1"), now=NOW)
    with pytest.raises(Reject, match="ALREADY_REVERSED"):
        apply_event(s, ev("e3", "r2", "REFUND", 10_000, orig="t1"), now=NOW)


def test_ac003_window_expiry_without_new_events():
    s = CustomerState("C1")
    apply_event(s, ev("e1", "t1", at=NOW - timedelta(days=80)), now=NOW)
    assert feats(s, NOW)["transactionCount90d"] == 1
    later = NOW + timedelta(days=15)  # original now 95 days old
    assert feats(s, later)["transactionCount90d"] == 0
    assert feats(s, later)["netSpend90d"] == 0


def test_rejects_non_positive_amount_and_future_timestamp():
    s = CustomerState("C1")
    with pytest.raises(Exception):
        apply_event(s, ev("e1", "t1", amount=0), now=NOW)
    with pytest.raises(Reject, match="FUTURE_TIMESTAMP"):
        apply_event(s, ev("e2", "t2", at=NOW + timedelta(days=1)), now=NOW)


def test_refund_follows_original_category_and_day():
    s = CustomerState("C1")
    old = NOW - timedelta(days=10)
    apply_event(s, ev("e1", "t1", at=old, cat="TRAVEL"), now=NOW)
    apply_event(s, ev("e2", "t2", cat="F&B", amount=20_000), now=NOW)
    apply_event(s, ev("e3", "r1", "REFUND", 50_000, at=NOW, orig="t1", cat="F&B"), now=NOW)
    f = feats(s)
    assert f["categoryMonetaryShare"]["TRAVEL"] > 0
    assert round(sum(f["categoryMonetaryShare"].values()), 6) == 1.0
    assert f["netSpend90d"] == 150_000 - 50_000 + 20_000
