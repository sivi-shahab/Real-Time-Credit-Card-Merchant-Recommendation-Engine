# ADR-0014 — Scaling ingestion out to 10 000 / 20 000 events per second

**Status:** Proposed · 2026-09-27 (sizing from local measurements; to be confirmed on
representative hardware with `scripts/loadtest.py kafka`)

## Context
The target is 10 000 transactions/s steady and 20 000/s burst. One consumer process
applies ~220–280 events/s: it is CPU-bound in Python (asyncio, the Redis client), not
waiting on Redis (release gate). The design already scales by partition: events are keyed
by `customerId`, each customer's events stay ordered within one partition, handling is
idempotent (ADR-0003), so any number of consumers in one group can share the topic.

What had not been measured is whether it *does* scale, and what stops it. Measured here
through the broker (`loadtest.py kafka`, 12 partitions, 16 000–64 000 events, fresh ids,
events spread over distinct customers, empty state each run, one 16-core WSL host shared
with Kafka, Redis and Postgres):

| consumers | events/s | per consumer |
|---|---|---|
| 1 | 278 | 278 |
| 2 | 298 | 149 |
| 4 | 687 | 172 |
| 8 | 804–837 | ~105 |
| 12 | 723 | ~60 |

At 8 consumers each used ~66% of a core; **Redis was saturated** (~125% CPU, its command
thread full). `HGETALL` of the customer state was half of Redis's CPU (106 µs a call):
every event reads the whole state hash, and half of its fields are the 180-day
transaction ledger (`t:` records kept to match refunds and reversals), of which an event
needs at most two. On this data the median state holds 28 fields; an active card
(~40 transactions a month) holds ~400, so production pays more per event, not less.
Postgres was at ~22% CPU and is not the limit. (One caveat learned the hard way: replaying
the same few customers many times grows their ledgers without bound and makes a run
slower than the one before; the tool spreads copies over distinct customers.)

## Decision
1. **Partitions: 96 on `cc.transactions`**, created before go-live (partitions can be
   added but never removed, and adding them moves customers between partitions). 96 divides
   by 1, 2, 3, 4, 6, 8, 12, 16, 24, 32 and 48, so consumers can be scaled in even steps,
   and covers the 20 000/s burst at ~210/s per consumer. Keyed by `customerId`, unchanged.
2. **One consumer process per partition at peak**, fewer off-peak: the group rebalances;
   handling is idempotent, so a rebalance costs time, not correctness. Size steady state at
   ~50 consumers, burst at 96. Autoscale on consumer lag (`kafka_consumergroup_lag`), not CPU.
3. **Take the transaction ledger out of the per-event read** before adding Redis capacity.
   Move `t:<txnId>` records from `state:<customer>` to their own hash `txn:{<customer>}`
   and read only the ids an event names (its own for the duplicate check, the original
   for a refund or reversal) with `HMGET` in the admission pipeline. The per-event
   `HGETALL` then covers buckets and hours only (bounded by the 90-day window). Expected:
   at least half of Redis's per-event CPU here, far more for active cards.
4. **Redis Cluster, keys hash-tagged by customer.** Even after 3, one Redis command thread
   will not carry 10 000 events/s (here ~1 ms of Redis CPU per event). Every per-customer
   key takes a `{customerId}` tag (`state:{C1}`, `txn:{C1}`, `recidx:{C1}`, the cache
   keys) so the save transaction and the admission pipeline stay within one slot. Two
   global keys must go: the `erased` set (every event checks it: one hot slot) becomes a
   per-customer tombstone key `erased:{C1}`, and the metrics hash becomes per-consumer
   counters summed at read. Start at 6 primaries (with 3) and resize with the measurement.
5. **Postgres through PgBouncer** (transaction pooling): 96 consumers x 4 connections plus
   the API exceed `max_connections`. asyncpg then needs `statement_cache_size=0`. The
   batched `transaction_log` insert (one transaction per consumer batch) stays.
6. **Feature-update messages coalesced per batch**: one `customer.features` message per
   customer per consumer batch (its latest version) instead of one per event. Consumers of
   that topic only need the latest version; at 10 000/s it halves broker writes.

## Consequences
- Throughput is then bounded by consumer CPU (~5 ms of Python per event, ~50 cores at
  10 000/s) and by how many Redis primaries are provisioned; both scale horizontally.
- The ledger split (3), hash tags and tombstones (4) and coalescing (6) are code changes
  in `rec/store/redis_store.py`, `rec/core/ledger.py` and `rec/stream/processor.py`, with a
  one-off migration of existing Redis state (or a rebuild from Postgres with
  `scripts/rebuild_state.py`, which already exists for Redis loss).
- A customer's events are sequential by design (ordering per customer). A single very
  active customer is therefore processed by one consumer; per-customer rate is far below
  any consumer's capacity, so this bounds latency for that customer, not throughput.
- Acceptance: `loadtest.py kafka` on production-like hardware sustains 10 000/s for 30
  minutes with consumer lag bounded, and 20 000/s for 5 minutes with lag recovered within
  10 minutes after, with the partition count and consumer, Redis and PgBouncer sizing above.
- Rejected: rewriting the consumer outside Python first. It would cut consumer cores, but
  the measured ceiling is Redis, which a faster consumer reaches sooner. Revisit if
  consumer cores dominate cost after 3 and 4.
