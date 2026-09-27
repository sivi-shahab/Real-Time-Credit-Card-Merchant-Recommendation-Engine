# ADR-0014 — Scaling ingestion out to 10 000 / 20 000 events per second

**Status:** Accepted · 2026-09-27. Decisions 3, 4 and 6 are built, and the tooling for 1
(`scripts/create_topics.py`) and 5 (`PG_STATEMENT_CACHE_SIZE`); the sizing in 1, 2 and 5
is the platform's, to be confirmed on representative hardware with
`scripts/loadtest.py kafka`.

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
   `scripts/create_topics.py` creates all three produced topics this way (replication 3,
   `min.insync.replicas` 2, `customer.features` compacted, DLQ kept 30 days) and only
   reports an existing topic that differs.
2. **One consumer process per partition at peak**, fewer off-peak: the group rebalances;
   handling is idempotent, so a rebalance costs time, not correctness. Size steady state at
   ~50 consumers, burst at 96. Autoscale on consumer lag (`kafka_consumergroup_lag`), not CPU.
3. **Take the transaction ledger out of the per-event read** before adding Redis capacity.
   `t:<txnId>` records moved from `state:<customer>` to hashes per month of the
   transaction day, `txn:{<customer>}:<YYYY-MM>`. An event reads only the ids it names
   (its own for the duplicate check, the original for a refund or reversal) with `HMGET`
   over the at most 7 months still in retention, in the admission pipeline; records past
   180 days are ignored exactly as the prune did, and each month's hash expires on its own
   (`EXPIREAT`, no per-field TTL, so no Redis 7.4 requirement). The per-event `HGETALL`
   now covers buckets and hours only.
4. **Redis Cluster, keys hash-tagged by customer.** Even after 3, one Redis command thread
   will not carry 10 000 events/s (here ~1 ms of Redis CPU per event). Every per-customer
   key takes a `{customerId}` tag (`state:{C1}`, `txn:{C1}:<month>`, `recidx:{C1}`,
   `rec:{C1}|...`, `served:{C1}:...`) so the save transaction stays within one slot. The
   `erased` set every event checked (one hot slot) became a per-customer tombstone
   `erased:{C1}`. The `metrics` hash stays: the stream writes it once per batch per
   consumer, not per event. `REDIS_CLUSTER=true` selects the cluster client. Start at 6
   primaries (with 3) and resize with the measurement.
5. **Postgres through PgBouncer** (transaction pooling): 96 consumers x 4 connections plus
   the API exceed `max_connections`. PgBouncer 1.21+ with prepared statements on (the
   default since 1.22) needs no change; older, or with them off, set
   `PG_STATEMENT_CACHE_SIZE=0` (checked both ways against PgBouncer 1.25 in transaction
   mode: the E2E suite fails without it there and passes with it). The migration job
   connects to Postgres directly. The batched `transaction_log` insert stays.
6. **Feature-update messages coalesced per batch**: one `customer.features` message per
   customer per consumer batch (its latest version) instead of one per event. Consumers of
   that topic only need the latest version; at 10 000/s it halves broker writes.

## Consequences
- Throughput is then bounded by consumer CPU (~5 ms of Python per event, ~50 cores at
  10 000/s) and by how many Redis primaries are provisioned; both scale horizontally.
- The key layout changed, so deploying it needs Redis rebuilt from Postgres once:
  `python scripts/rebuild_state.py --flush` (the tool that already exists for Redis
  loss). Old `state:<id>` keys are never read again; the flush removes them.
- The whole test suite passes against a 3-primary Redis Cluster as well as a single Redis.
- Measured after 3, 4 and 6, same benchmark: 1 consumer 278 → 346 events/s, 8 consumers
  837 → 1 070 events/s; Redis CPU at 8 consumers ~125% → ~51%, `HGETALL` 106 → 17 µs a
  call. Redis is no longer the limit here; the single local Kafka broker (~200% CPU) and
  the consumers are, and both scale out.
- redis-py 8.1 passes a cluster transaction's routing `keys` to response callbacks that do
  not take it; `rec/store/redis_store.py` wraps the callbacks. Remove the wrapper once
  redis-py fixes it.
- A customer's events are sequential by design (ordering per customer). A single very
  active customer is therefore processed by one consumer; per-customer rate is far below
  any consumer's capacity, so this bounds latency for that customer, not throughput.
- Acceptance: `loadtest.py kafka` on production-like hardware sustains 10 000/s for 30
  minutes with consumer lag bounded, and 20 000/s for 5 minutes with lag recovered within
  10 minutes after, with the partition count and consumer, Redis and PgBouncer sizing above.
- Rejected: rewriting the consumer outside Python first. It would cut consumer cores, but
  the measured ceiling is Redis, which a faster consumer reaches sooner. Revisit if
  consumer cores dominate cost after 3 and 4.
