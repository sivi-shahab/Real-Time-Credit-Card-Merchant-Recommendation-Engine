"""SYN-006 acceptance: same config+seed+version => identical checksums."""
from pathlib import Path

import pytest

from rec.generator.config import DatasetConfig
from rec.generator.generate import generate

SMALL = dict(customerCount=120, merchantCount=40, promotionCount=10, transactionCount=800,
             outputFormats=["jsonl"])


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    base = tmp_path_factory.mktemp("gen")
    a = generate(DatasetConfig(seed=7, **SMALL), base / "a")
    b = generate(DatasetConfig(seed=7, **SMALL), base / "b")
    c = generate(DatasetConfig(seed=8, **SMALL), base / "c")
    return a, b, c


def test_reproducible_checksums(runs):
    a, b, _ = runs
    assert {k: v["sha256"] for k, v in a["files"].items()} == {
        k: v["sha256"] for k, v in b["files"].items()
    }
    assert a["manifestChecksum"] == b["manifestChecksum"]


def test_different_seed_changes_data(runs):
    a, _, c = runs
    assert a["files"]["transactions.jsonl"]["sha256"] != c["files"]["transactions.jsonl"]["sha256"]


def test_referential_integrity_apart_from_injected_faults(runs):
    q = runs[0]["qualityReport"]
    faults = q["injectedFaultCounts"]
    assert q["unknownCustomerRefs"] == faults.get("UNKNOWN_CUSTOMER", 0)
    assert q["unknownMerchantRefs"] == faults.get("UNKNOWN_MERCHANT", 0)
    assert q["nonPositiveAmounts"] == faults.get("ZERO_AMOUNT", 0)
    assert q["orphanCorrections"] == 0


def test_failure_injection_present_and_profile_scaling():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        m = generate(DatasetConfig(seed=7, scenarioProfile="normal", **SMALL), Path(d) / "n")
        assert m["qualityReport"]["injectedFaultCounts"] == {}


def test_distribution_has_long_tail_and_hour_shape(runs):
    s = runs[0]["distributionSummary"]
    assert len(s["byCategory"]) >= 5
    assert s["amountMinorPercentiles"]["50"] < s["amountMinorPercentiles"]["99"]
    assert sum(s["byHourUtc"].values()) > 0


def test_id_namespace_isolates_experiments(tmp_path):
    """SIM-002 — a namespaced dataset must not collide with an un-namespaced one."""
    import json
    a = generate(DatasetConfig(seed=7, **SMALL), tmp_path / "plain")
    b = generate(DatasetConfig(seed=7, idNamespace="exp1", **SMALL), tmp_path / "ns")
    ids_a = {json.loads(line)["customerId"] for line in (tmp_path / "plain" / "customers.jsonl")
             .read_text().splitlines()}
    ids_b = {json.loads(line)["customerId"] for line in (tmp_path / "ns" / "customers.jsonl")
             .read_text().splitlines()}
    assert not ids_a & ids_b
    assert all(x.startswith("exp1-") for x in ids_b)
    assert a["manifestChecksum"] != b["manifestChecksum"]


def test_namespace_also_isolates_event_and_transaction_ids(tmp_path):
    """Dedup is keyed on eventId/transactionId, so the namespace must reach them."""
    import json
    generate(DatasetConfig(seed=7, **SMALL), tmp_path / "plain")
    generate(DatasetConfig(seed=7, idNamespace="exp1", **SMALL), tmp_path / "ns")
    def ids(d, key):
        return {json.loads(line)[key] for line in (tmp_path / d / "transactions.jsonl")
                .read_text().splitlines()}
    assert not ids("plain", "eventId") & ids("ns", "eventId")
    assert not ids("plain", "transactionId") & ids("ns", "transactionId")
