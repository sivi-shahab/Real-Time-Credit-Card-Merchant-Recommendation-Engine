"""Feature Engine — the consumer that turns raw transactions into online features.

Pure logic lives in rec.core.ledger; this module is transport + persistence only,
so the offline oracle (scripts/reconcile.py) runs the identical arithmetic.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from prometheus_client import start_http_server
from pydantic import ValidationError

from rec.core.ledger import LATE_ARRIVAL_LIMIT_HOURS, Reject, apply_event
from rec.core.models import Envelope
from rec.obs import EVENTS, kafka_context, kafka_headers, setup_logging, setup_tracing
from rec.settings import settings
from rec.store import pg
from rec.store.redis_store import OnlineStore

log = logging.getLogger("stream")
_tracer = trace.get_tracer("rec.stream")


class FeatureProcessor:
    """Handles one event. Ordering per customer comes from Kafka keying."""

    def __init__(self, store: OnlineStore, producer: AIOKafkaProducer | None = None, *,
                 batch: bool = False):
        """batch=True buffers transaction_log rows and counters until flush(); the Kafka
        loop flushes before committing offsets. Otherwise every handle() flushes."""
        self.store = store
        self.producer = producer
        self.batch = batch
        self._known_customers: set[str] = set()
        self._log_rows: list[tuple] = []
        self._counts: Counter[str] = Counter()
        self._feature_versions: dict[str, int] = {}  # customer -> latest, sent at flush

    async def handle(self, raw: dict, *, now: datetime | None = None) -> str:
        outcome = await self._handle(raw, now or datetime.now(UTC))
        if not self.batch:
            await self.flush()
        return outcome

    async def handle_message(self, raw: dict, headers=None, *,
                             now: datetime | None = None) -> str:
        """handle(), in a consumer span that continues the producer's trace from the
        message headers, so the Redis and Postgres work nests under it."""
        with _tracer.start_as_current_span(
                f"{settings.topic_transactions} process", context=kafka_context(headers),
                kind=SpanKind.CONSUMER,
                attributes={"messaging.system": "kafka",
                            "messaging.destination.name": settings.topic_transactions,
                            "messaging.message.id": str(raw.get("eventId", ""))}) as span:
            outcome = await self.handle(raw, now=now)
            span.set_attribute("rec.outcome", outcome)
            return outcome

    async def flush(self) -> None:
        # ponytail: Redis state is written before its log row is flushed, so a crash in
        # between re-logs those events as DUPLICATE_EVENT on redelivery. Features stay
        # exact; only the log outcome is off. Outbox table if that audit trail must be exact.
        rows, self._log_rows = self._log_rows, []
        counts, self._counts = self._counts, Counter()
        versions, self._feature_versions = self._feature_versions, {}
        await pg.log_transactions(rows)
        await self.store.incr_metrics(counts)
        for customer_id, version in versions.items():
            await self._emit_feature_update(customer_id, version)

    def _log(self, raw: dict, outcome: str, code: str | None = None,
             detail: str | None = None) -> None:
        self._log_rows.append(pg.transaction_row(raw, outcome, code, detail))

    async def _handle(self, raw: dict, now: datetime) -> str:
        payload = raw.get("payload")
        raw_customer = payload.get("customerId") if isinstance(payload, dict) else None
        try:
            env = Envelope.model_validate(raw)
            env.transaction()
            invalid = None
        except ValidationError as exc:
            env, invalid = None, exc
        # Known before claiming: a claim for an unknown customer would turn the DLQ replay
        # of that event, once the customer exists, into a DUPLICATE_EVENT.
        known = env is not None and await self._customer_known(env.payload["customerId"])
        # The ledger records this event can touch: its own transaction (a replay is a
        # duplicate) and the original a refund or reversal corrects (ADR-0014).
        txn_ids = [] if env is None else [
            t for t in (env.payload.get("transactionId"),
                        env.payload.get("originalTransactionId")) if isinstance(t, str) and t]
        erased, claimed, state = await self.store.admit(
            raw_customer if isinstance(raw_customer, str) else None,
            env.eventId if known else None, txn_ids, as_of=now)
        # AC-009 before anything else, invalid events included: quarantining logs the
        # raw envelope, which would re-store the very data that was erased.
        if erased:
            self._counts["events_erased_customer"] += 1
            return "ERASED_CUSTOMER"
        if invalid is not None:
            await self._quarantine(raw, "SCHEMA_INVALID", str(invalid)[:500])
            return "SCHEMA_INVALID"

        customer_id = env.payload["customerId"]
        if not known:
            if customer_id in await pg.erased_customers():
                # Redis lost its tombstone copy (e.g. before a rebuild): Postgres decides.
                await self.store.load_tombstones([customer_id])
                self._counts["events_erased_customer"] += 1
                return "ERASED_CUSTOMER"
            await self._quarantine(raw, "UNKNOWN_CUSTOMER", customer_id)
            return "UNKNOWN_CUSTOMER"

        if not claimed:
            self._log(raw, "DUPLICATE_EVENT")
            self._counts["events_duplicate"] += 1
            return "DUPLICATE_EVENT"

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
            self._counts["events_late_backfill"] += 1

        await self.store.save(state, now)  # also drops cached recommendations (SERV-004)
        self._log(raw, outcome)
        self._counts["events_applied" if outcome == "APPLIED" else "events_duplicate"] += 1
        # one feature update per customer per batch, its latest version (ADR-0014):
        # consumers of the topic only need the newest
        self._feature_versions[customer_id] = state.featureVersion
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
        self._log(raw, "QUARANTINED", code, detail)
        self._counts["events_quarantined"] += 1
        self._counts[f"quarantine_{code}"] += 1
        if self.producer:
            await self.producer.send_and_wait(
                settings.topic_dlq,
                # contracts/avro/rejected_event.avsc: the rejected message verbatim, as text
                json.dumps({"rejectCode": code, "detail": detail,
                            "event": json.dumps(raw, default=str)}).encode(),
                key=str(raw.get("eventId", "")).encode(),
                headers=kafka_headers(),
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
            headers=kafka_headers(),
        )


async def run() -> None:
    setup_logging()
    setup_tracing("rec-stream")
    start_http_server(settings.stream_metrics_port)  # Prometheus scrape for the consumer
    store = OnlineStore()
    consumer = AIOKafkaConsumer(
        settings.topic_transactions,
        bootstrap_servers=settings.kafka_bootstrap,
        group_id=settings.stream_group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap,
                                enable_idempotence=True, acks="all")
    await store.load_tombstones(await pg.erased_customers())
    await consumer.start()
    await producer.start()
    processor = FeatureProcessor(store, producer, batch=True)
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
            by_customer: dict[str, list[tuple[dict, list]]] = {}
            for records in batches.values():
                for msg in records:
                    try:
                        raw = json.loads(msg.value)
                    except Exception:
                        raw = None
                    if not isinstance(raw, dict):  # not JSON, or JSON but no envelope
                        await store.incr_metric("undecodable_messages")
                        continue
                    payload = raw.get("payload")
                    key = str(payload.get("customerId", "")) if isinstance(payload, dict) else ""
                    by_customer.setdefault(key, []).append((raw, msg.headers))

            async def run(events: list[tuple[dict, list]]) -> None:
                async with gate:
                    await _drain(events)

            async def _drain(events: list[tuple[dict, list]]) -> None:
                for raw, headers in events:
                    try:
                        EVENTS.labels(await processor.handle_message(raw, headers)).inc()
                    except Exception:  # keep the consumer alive; the event is logged
                        log.exception("handler failed for event %s", raw.get("eventId"))
                        await store.incr_metric("handler_errors")

            await asyncio.gather(*(run(events) for events in by_customer.values()))
            with _tracer.start_as_current_span("transaction_log flush"):
                await processor.flush()  # log rows land before the offsets move
            # At-least-once: handling is idempotent, so a replayed batch is a no-op.
            await consumer.commit()
    finally:
        await consumer.stop()
        await producer.stop()
        await store.close()
        await pg.close()


if __name__ == "__main__":
    asyncio.run(run())
