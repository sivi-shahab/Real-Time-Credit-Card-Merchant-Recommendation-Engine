"""The broker load test must drive the real apply path: fresh ids, intact refund chains."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "loadtest", Path(__file__).resolve().parents[1] / "scripts" / "loadtest.py")
loadtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loadtest)


def test_replay_copies_are_new_events_with_their_own_refund_chains():
    purchase = {"eventId": "e1", "payload": {"customerId": "C1", "transactionId": "t1"}}
    refund = {"eventId": "e2", "payload": {"customerId": "C1", "transactionId": "t2",
                                           "originalTransactionId": "t1"}}
    lines = [json.dumps(purchase), json.dumps(refund)]

    out = list(loadtest.replay_copies(lines, 2, "run", distinct_customers=True))
    assert len(out) == 4
    assert len({e["eventId"] for e in out}) == 4, "fresh event ids: applied, not deduped"
    for copy in (out[:2], out[2:]):
        bought, refunded = copy
        assert refunded["payload"]["originalTransactionId"] == \
            bought["payload"]["transactionId"], "a refund names its own copy's purchase"
        assert bought["payload"]["customerId"] == refunded["payload"]["customerId"]
    assert out[0]["payload"]["customerId"] != out[2]["payload"]["customerId"]
    same = list(loadtest.replay_copies(lines, 2, "run", distinct_customers=False))
    assert {e["payload"]["customerId"] for e in same} == {"C1"}
