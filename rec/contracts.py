"""Avro contracts for Kafka messages (SDD 2.1, EVT-001, EVT-004).

The wire is JSON today (ADR-0001); the Avro schemas in contracts/avro are the contract.
`to_record` maps a wire message onto its schema and `round_trip` pushes it through Avro
binary, so a test proves every message we produce is representable exactly — switching
the wire to Avro is then a serializer change, not a contract change.
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import fastavro
from fastavro.utils import generate_many

AVRO_DIR = Path(__file__).resolve().parents[1] / "contracts" / "avro"
TOPIC_SCHEMAS = {  # EVT-002 topics that are actually produced
    "cc.transactions": "transaction_event",
    "customer.features": "feature_update",
    "pipeline.dlq": "rejected_event",
}


def parse(schema_json: str | dict) -> dict:
    raw = json.loads(schema_json) if isinstance(schema_json, str) else schema_json
    return fastavro.parse_schema(raw)


def load(name: str) -> dict:
    return parse((AVRO_DIR / f"{name}.avsc").read_text())


def _convert(value, avro_type):
    """JSON wire value -> Avro datum for one field type (timestamps are ISO strings on
    the wire, timestamp-millis in Avro). Unknown wire fields are dropped by the caller."""
    if isinstance(avro_type, list):  # union: null or the one concrete branch
        if value is None:
            return None
        concrete = next(t for t in avro_type if t != "null")
        return _convert(value, concrete)
    if isinstance(avro_type, dict):
        if avro_type.get("logicalType") == "timestamp-millis" and isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        if avro_type.get("type") == "record":
            return {f["name"]: _convert(value.get(f["name"]), f["type"])
                    for f in avro_type["fields"] if f["name"] in value or "default" not in f}
    return value


def to_record(message: dict, schema: dict) -> dict:
    return {f["name"]: _convert(message.get(f["name"]), f["type"])
            for f in schema["fields"] if f["name"] in message or "default" not in f}


def round_trip(message: dict, schema: dict) -> dict:
    """Validate strictly, encode to Avro binary, decode. Raises on any mismatch."""
    record = to_record(message, schema)
    fastavro.validation.validate(record, schema, raise_errors=True, strict=True)
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, record)
    buf.seek(0)
    return fastavro.schemaless_reader(buf, schema, schema)


def backward_compatible(old: dict, new: dict, samples: int = 200) -> str | None:
    """EVT-004 BACKWARD: can a reader on `new` read data written with `old`?
    Returns None when compatible, else the resolution error. Checked by actually
    resolving generated old-schema data, the same way a consumer would."""
    try:
        for datum in generate_many(old, samples):
            buf = io.BytesIO()
            fastavro.schemaless_writer(buf, old, datum)
            buf.seek(0)
            fastavro.schemaless_reader(buf, old, new)
    except Exception as exc:  # noqa: BLE001 - any resolution failure is the answer
        return f"{type(exc).__name__}: {exc}"
    return None
