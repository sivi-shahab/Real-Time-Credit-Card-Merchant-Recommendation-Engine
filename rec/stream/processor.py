"""Feature Engine — the consumer that turns raw transactions into online features.

Pure logic lives in rec.core.ledger; this module is transport + persistence only,
so the offline oracle (scripts/reconcile.py) runs the identical arithmetic.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from pydantic import ValidationError

from rec.core.ledger import LATE_ARRIVAL_LIMIT_HOURS, Reject, apply_event
from rec.core.models import Envelope
from rec.settings import settings
from rec.store import pg
from rec.store.redis_store import OnlineStore

log = logging.getLogger("stream")


class FeatureProcessor:
    """Handles one event. Ordering per customer comes from Kafka keying."""

    def __init__(self, store: OnlineStore, producer: AIOKafkaProducer | None = None):
        self.store = store
        self.producer = producer
        self._known_customers: set[str] = set()

    async def handle(self, raw: dict, *, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        try:
            env = Envelope.model_validate(raw)
            env.transaction()
        except ValidationError as exc:
            await self._quarantine(raw, "SCHEMA_INVALID", str(exc)[:500])
            return "SCHEMA_INVALID"

        customer_id = env.payload["customerId"]
        if not await self._customer_known(customer_id):
            await self._quarantine(raw, "UNKNOWN_CUSTOMER", customer_id)
            return "UNKNOWN_CUSTOMER"

        if not await self.store.claim_event(env.eventId):
            await pg.log_transaction(raw, "DUPLICATE_EVENT")
            await self.store.incr_metric("events_duplicate")
            return "DUPLICATE_EVENT"

        state = await self.store.load(customer_id)
        try:
            outcome = apply_event(state, env, now=now)
        except Reject as exc:
            await self.store.release_event(env.eventId)
            await self._quarantine(raw, exc.code, exc.detail)
            return exc.code
        except AssertionError as exc:
            await self.store.release_event(env.eventId)
            await self._quarantine(raw, "INVARIANT_VIOLATION", str(exc))
            return "INVARIANT_VIOLATION"

        lateness = now - env.occurredAt.astimezone(UTC)
        if lateness > timedelta(hours=LATE_ARRIVAL_LIMIT_HOURS):
            await self.store.incr_metric("events_late_backfill")

        await self.store.save(state, now)
        await self.store.invalidate_customer(customer_id)  # SERV-004
        await pg.log_transaction(raw, outcome)
        await self.store.incr_metric("events_applied" if outcome == "APPLIED"
                                     else "events_duplicate")
        await self._emit_feature_update(customer_id, state.featureVersion)
        return outcome

    async def _customer_known(self, customer_id: str) -> bool:
        """Reference check on the hot path — cached, since customers are not
        deleted mid-stream and an unknown id is re-checked every time anyway."""
        if customer_id in self._known_customers:
            return True
        if await pg.customer(customer_id) is None:
            return False
        self._known_customers.add(customer_id)
        return True

    async def _quarantine(self, raw: dict, code: str, detail: str) -> None:
        await pg.log_transaction(raw, "QUARANTINED", code, detail)
        await self.store.incr_metric("events_quarantined")
        await self.store.incr_metric(f"quarantine_{code}")
        if self.producer:
            await self.producer.send_and_wait(
                settings.topic_dlq,
                json.dumps({"rejectCode": code, "detail": detail, "event": raw},
                           default=str).encode(),
                key=str(raw.get("eventId", "")).encode(),
            )

    async def _emit_feature_update(self, customer_id: str, version: int) -> None:
        """Fire-and-forget: the online store is already updated, so this notification
        is derived state. Awaiting acks=all per event costs a full broker round trip."""
        if not self.producer:
            return
        await self.producer.send(
            settings.topic_features,
            json.dumps({"customerId": customer_id, "featureVersion": version,
                        "producedAt": datetime.now(UTC).isoformat()}).encode(),
            key=customer_id.encode(),
        )


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    store = OnlineStore()
    consumer = AIOKafkaConsumer(
        settings.topic_transactions,
        bootstrap_servers=settings.kafka_bootstrap,
        group_id="feature-engine",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap,
                                enable_idempotence=True, acks="all")
    await consumer.start()
    await producer.start()
    processor = FeatureProcessor(store, producer)
    log.info("feature engine consuming %s", settings.topic_transactions)
    # Bounded: unbounded gather exhausts the Redis and Postgres pools.
    gate = asyncio.Semaphore(settings.stream_concurrency)
    try:
        while True:
            batches = await consumer.getmany(timeout_ms=500, max_records=500)
            if not batches:
                continue
            # Ordering only has to hold per customer, so customers run concurrently
            # while each customer's own events stay strictly sequential.
            by_customer: dict[str, list[dict]] = {}
            for records in batches.values():
                for msg in records:
                    try:
                        raw = json.loads(msg.value)
                    except Exception:
                        await store.incr_metric("undecodable_messages")
                        continue
                    key = str(raw.get("payload", {}).get("customerId", ""))
                    by_customer.setdefault(key, []).append(raw)

            async def run(events: list[dict]) -> None:
                async with gate:
                    await _drain(events)

            async def _drain(events: list[dict]) -> None:
                for raw in events:
                    try:
                        await processor.handle(raw)
                    except Exception:  # keep the consumer alive; the event is logged
                        log.exception("handler failed for event %s", raw.get("eventId"))
                        await store.incr_metric("handler_errors")

            await asyncio.gather(*(run(events) for events in by_customer.values()))
            # At-least-once: handling is idempotent, so a replayed batch is a no-op.
            await consumer.commit()
    finally:
        await consumer.stop()
        await producer.stop()
        await store.close()
        await pg.close()


if __name__ == "__main__":
    asyncio.run(run())
