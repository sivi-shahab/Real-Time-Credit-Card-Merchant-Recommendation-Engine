"""Topic provisioning plans production settings and never silently changes a topic."""
import importlib.util
import sys
from pathlib import Path

from rec.contracts import TOPIC_SCHEMAS

spec = importlib.util.spec_from_file_location(
    "create_topics", Path(__file__).resolve().parents[1] / "scripts" / "create_topics.py")
create_topics = importlib.util.module_from_spec(spec)
sys.modules["create_topics"] = create_topics  # its dataclass looks itself up there
spec.loader.exec_module(create_topics)


def test_every_produced_topic_is_planned_with_durable_settings():
    specs = {s.name: s for s in create_topics.topic_specs(96, 3)}
    assert set(specs) == set(TOPIC_SCHEMAS), "a produced topic is not provisioned"
    assert specs["cc.transactions"].partitions == 96
    assert specs["customer.features"].configs["cleanup.policy"] == "compact"
    assert all(s.configs["min.insync.replicas"] == "2" and s.replication_factor == 3
               for s in specs.values())
    # a single-broker dev cluster cannot require two in-sync replicas
    assert all(s.configs["min.insync.replicas"] == "1"
               for s in create_topics.topic_specs(12, 1))


def test_an_existing_topic_is_compared_not_changed():
    planned = create_topics.topic_specs(96, 3)[0]
    assert create_topics.compare(planned, 96, 3, dict(planned.configs)) == []
    diffs = create_topics.compare(planned, 6, 1, {"cleanup.policy": "delete"})
    assert "partitions 6, planned 96" in diffs
    assert any(d.startswith("replication factor 1") for d in diffs)
    assert any(d.startswith("retention.ms=None") for d in diffs)
