# ADR-0003 — At-least-once delivery with idempotent handling

**Status:** Accepted · 2026-09-24

## Context
EVT-003 asks for `exactly_once_v2` on the Kafka Streams path. The Redis sink is outside
any Kafka transaction regardless, so it needs conditional/idempotent updates either way.

## Decision
The consumer commits offsets per batch after processing. Duplicate suppression uses two
independent keys: `eventId` (Redis SETNX with a 7-day TTL) and business transaction
identity (`transactionId` in the ledger). A redelivered batch is therefore a no-op.

## Consequences
- A crash between processing and commit replays the batch; the replay is verified to
  apply nothing (`test_ac001_full_replay_is_idempotent`).
- Quarantine is not state: a rejected event releases its dedup claim and is rejected
  again with the same code on replay.
- Deduplication is bounded by the dedup TTL. An event redelivered after 7 days would be
  reapplied at the envelope level, but the transaction-identity check still stops it.
