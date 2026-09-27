#!/usr/bin/env python
"""Create the Kafka topics the pipeline produces, with production settings (ADR-0014).

Idempotent. An existing topic is never changed, only compared: adding partitions moves
customers to other partitions and breaks per-customer ordering while old and new overlap,
so it is a planned operation, not something a provisioning run does on its own.

    python scripts/create_topics.py --bootstrap kafka:9092 --partitions 96 \
        --replication-factor 3 [--dry-run]

Exit code 1 if an existing topic differs from the plan (reported, left alone).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rec.settings import settings  # noqa: E402

DAY_MS = 24 * 3600 * 1000


@dataclass
class TopicSpec:
    name: str
    partitions: int
    replication_factor: int
    configs: dict[str, str] = field(default_factory=dict)


def topic_specs(partitions: int, replication_factor: int,
                retention_days: int = 7) -> list[TopicSpec]:
    """What each produced topic (rec/contracts.py TOPIC_SCHEMAS) should look like."""
    durable = {"min.insync.replicas": str(min(2, replication_factor))}
    return [
        # keyed by customerId: partitions bound the consumers that can share the load
        TopicSpec(settings.topic_transactions, partitions, replication_factor, durable | {
            "cleanup.policy": "delete",
            # replays past this rely on the ledger's transactionId check, not eventId
            "retention.ms": str(retention_days * DAY_MS)}),
        # consumers only need each customer's latest version: keep just that
        TopicSpec(settings.topic_features, partitions, replication_factor, durable | {
            "cleanup.policy": "compact"}),
        # rejected events are read by people, slowly: few partitions, kept longer
        TopicSpec(settings.topic_dlq, min(partitions, 6), replication_factor, durable | {
            "cleanup.policy": "delete", "retention.ms": str(30 * DAY_MS)}),
    ]


def compare(spec: TopicSpec, partitions: int, replication_factor: int,
            configs: dict[str, str]) -> list[str]:
    """How an existing topic differs from its spec, in words; empty when it matches."""
    diffs = []
    if partitions != spec.partitions:
        diffs.append(f"partitions {partitions}, planned {spec.partitions}")
    if replication_factor != spec.replication_factor:
        diffs.append(f"replication factor {replication_factor}, planned "
                     f"{spec.replication_factor}")
    diffs += [f"{key}={configs.get(key)}, planned {value}"
              for key, value in spec.configs.items() if configs.get(key) != value]
    return diffs


async def main(args) -> int:
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType

    specs = topic_specs(args.partitions, args.replication_factor, args.retention_days)
    admin = AIOKafkaAdminClient(bootstrap_servers=args.bootstrap)
    await admin.start()
    report, drift = {}, False
    try:
        existing = set(await admin.list_topics())
        missing = [s for s in specs if s.name not in existing]
        for spec in (s for s in specs if s.name in existing):
            meta = (await admin.describe_topics([spec.name]))[0]
            parts = meta["partitions"]
            described = await admin.describe_configs(
                [ConfigResource(ConfigResourceType.TOPIC, spec.name)])
            configs = {entry[0]: entry[1] for entry in described[0].resources[0][4]}
            diffs = compare(spec, len(parts), len(parts[0]["replicas"]) if parts else 0,
                            configs)
            report[spec.name] = {"status": "exists", "differs": diffs}
            drift = drift or bool(diffs)
        if missing and not args.dry_run:
            await admin.create_topics([
                NewTopic(s.name, s.partitions, s.replication_factor, topic_configs=s.configs)
                for s in missing])
        for spec in missing:
            report[spec.name] = {"status": "would create" if args.dry_run else "created",
                                 "partitions": spec.partitions,
                                 "replicationFactor": spec.replication_factor,
                                 "configs": spec.configs}
    finally:
        await admin.close()
    print(json.dumps(report, indent=2))
    return 1 if drift else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", default=settings.kafka_bootstrap)
    parser.add_argument("--partitions", type=int, default=96)
    parser.add_argument("--replication-factor", type=int, default=3)
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
