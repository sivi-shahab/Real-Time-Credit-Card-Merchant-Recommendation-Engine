"""SDD 16 contract level: what we actually put on Kafka matches contracts/avro, and the
compatibility check really rejects breaking changes (EVT-004)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import yaml

from rec import contracts
from rec.generator.config import DatasetConfig
from rec.generator.generate import generate
from rec.stream.processor import FeatureProcessor

REF = datetime(2026, 9, 23, tzinfo=UTC)


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    out = tmp_path_factory.mktemp("contract-ds")
    generate(DatasetConfig(seed=5, referenceTime=REF, customerCount=40, merchantCount=20,
                           promotionCount=5, transactionCount=600, outputFormats=["jsonl"]),
             out)
    return [json.loads(line) for line in (out / "replay.jsonl").read_text().splitlines()]


def test_every_valid_generated_transaction_fits_the_avro_contract(replay):
    schema = contracts.load("transaction_event")
    clean = [e for e in replay if not e.get("injectedFault")]
    assert len(clean) > 500
    types = set()
    for event in clean:
        decoded = contracts.round_trip(event, schema)
        assert decoded["payload"]["amountMinor"] == event["payload"]["amountMinor"]
        types.add(decoded["payload"]["transactionType"])
    assert types == {"PURCHASE", "REFUND", "REVERSAL"}


def test_contract_rejects_what_the_domain_rejects():
    schema = contracts.load("transaction_event")
    event = {"eventId": "a5ab7754-0065-5dbc-9676-5698b4e30574", "eventType": "transaction.created",
             "eventVersion": "1.0.0", "occurredAt": "2026-03-28T04:33:00+00:00",
             "producer": "t", "payload": {
                 "transactionId": "t1", "customerId": "C1", "merchantId": "M1",
                 "transactionType": "PURCHASE", "amountMinor": 1000, "currency": "IDR",
                 "occurredAt": "2026-03-28T04:33:00+00:00"}}
    contracts.round_trip(event, schema)
    for field, bad in (("transactionType", "CHARGEBACK"), ("currency", "USD"),
                       ("amountMinor", 10.5), ("customerId", None)):
        broken = json.loads(json.dumps(event))
        broken["payload"][field] = bad
        with pytest.raises(Exception):
            contracts.round_trip(broken, schema)


class _Capture:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send(self, topic, value, key=None, headers=None):
        self.sent.append((topic, json.loads(value)))

    send_and_wait = send


async def test_stream_producers_emit_contract_messages(replay):
    """Feature updates and DLQ records built by the real processor code."""
    producer = _Capture()
    processor = FeatureProcessor(store=None, producer=producer)
    await processor._emit_feature_update("C0000001", 7)
    faulty = next(e for e in replay if e.get("injectedFault"))
    await processor._quarantine(faulty, "SCHEMA_INVALID", "bad")
    assert {t for t, _ in producer.sent} == {"customer.features", "pipeline.dlq"}
    for topic, message in producer.sent:
        contracts.round_trip(message, contracts.load(contracts.TOPIC_SCHEMAS[topic]))


def test_compatibility_check_rejects_breaking_changes():
    old = contracts.load("feature_update")
    raw = json.loads((contracts.AVRO_DIR / "feature_update.avsc").read_text())

    def variant(mutate) -> dict:
        copy = json.loads(json.dumps(raw))
        mutate(copy["fields"])
        return contracts.parse(copy)

    ok = variant(lambda f: f.append({"name": "source", "type": "string", "default": "stream"}))
    assert contracts.backward_compatible(old, ok) is None
    no_default = variant(lambda f: f.append({"name": "source", "type": "string"}))
    assert contracts.backward_compatible(old, no_default)
    retyped = variant(lambda f: f[1].update(type="string"))
    assert contracts.backward_compatible(old, retyped)


def test_asyncapi_names_a_schema_for_every_produced_topic():
    doc = yaml.safe_load((contracts.AVRO_DIR.parent / "asyncapi.yaml").read_text())
    addresses = {c["address"]: c for c in doc["channels"].values()}
    messages = doc["components"]["messages"]
    for topic, name in contracts.TOPIC_SCHEMAS.items():
        ref = next(iter(addresses[topic]["messages"].values()))["$ref"]
        payload = messages[ref.rsplit("/", 1)[-1]]["payload"]
        assert payload["schema"]["$ref"] == f"./avro/{name}.avsc", topic
        assert (contracts.AVRO_DIR / f"{name}.avsc").exists()
