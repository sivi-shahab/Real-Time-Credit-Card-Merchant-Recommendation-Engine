from datetime import timedelta

import pytest

from rec.core.features import _normalised, compute_features
from rec.core.ledger import CustomerState, apply_event
from tests.test_ledger import NOW, ev


def test_interest_formula_matches_spec():
    s = CustomerState("C1")
    apply_event(s, ev("e1", "t1", amount=100_000, cat="F&B"), now=NOW)
    apply_event(s, ev("e2", "t2", amount=300_000, cat="TRAVEL", merch="M2"), now=NOW)
    f = compute_features(s, NOW)
    # same-day: recency == 1 for both; F&B -> 0.4*0.5 + 0.3*0.25 + 0.3*1
    assert f["categoryInterest"]["F&B"] == pytest.approx(0.4 * 0.5 + 0.3 * 0.25 + 0.3 * 1.0)
    assert f["categoryInterest"]["TRAVEL"] == pytest.approx(0.4 * 0.5 + 0.3 * 0.75 + 0.3 * 1.0)


def test_recency_decays_and_absent_category_is_zero():
    s = CustomerState("C1")
    apply_event(s, ev("e1", "t1", at=NOW - timedelta(days=30)), now=NOW)
    f = compute_features(s, NOW)
    assert f["categoryRecency"]["F&B"] == pytest.approx(pytest.approx(0.36787944, abs=1e-6))
    assert "TRAVEL" not in f["categoryRecency"]


def test_cold_start_flag_and_empty_customer():
    f = compute_features(CustomerState("NEW"), NOW)
    assert f["coldStartFlag"] and f["transactionCount90d"] == 0
    assert f["categoryInterest"] == {} and f["daysSinceLastTransaction"] is None


def test_weights_are_normalised_and_validated():
    assert _normalised({"a": 2, "b": 2}) == {"a": 0.5, "b": 0.5}
    with pytest.raises(ValueError):
        _normalised({"a": -1, "b": 2})
    with pytest.raises(ValueError):
        _normalised({"a": 0})


def test_windows_are_nested_correctly():
    s = CustomerState("C1")
    for i, d in enumerate([0, 3, 20, 60]):
        apply_event(s, ev(f"e{i}", f"t{i}", at=NOW - timedelta(days=d)), now=NOW)
    w = compute_features(s, NOW)["windows"]
    assert [w["1"]["transactionCount"], w["7"]["transactionCount"],
            w["30"]["transactionCount"], w["90"]["transactionCount"]] == [1, 2, 3, 4]
