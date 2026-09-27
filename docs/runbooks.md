# Operational runbooks

Every Prometheus alert in `deploy/prometheus/alerts.yml` links to a section here. Commands
assume the compose stack; on Kubernetes replace `docker compose exec <svc>` with
`kubectl exec deploy/<svc>`. Correlate everything by `traceId`: it is on every API error
body, the `x-correlation-id` response header, every JSON log line and every audit row.

## service-down
`up == 0` for a scrape target.
1. `docker compose ps` — is the container running, restarting, unhealthy?
2. `docker compose logs --tail 200 <svc>` — logs are JSON; filter with `jq 'select(.level=="ERROR")'`.
3. API down → customers get nothing; restart it. Ranking down → serving degrades to the
   baseline on its own (no customer impact). Stream down → features go stale; events wait
   in Kafka and are consumed on restart (at-least-once, idempotent).

## api-latency
Recommendation p95 > 200 ms for 10 min.
1. Check `recommendations_total` by `source`: a drop in `CACHE` share means cache
   invalidation churn (a promotion or catalog change invalidates everything — SERV-004).
2. Check `ranking_inference_seconds` and the guardrail status
   (`GET /admin/v1/models/guardrail/status`). If the model arm is slow, roll back (below).
   `ranking_degraded_total{reason="RANKING_TIMEOUT"}` rising while inference stays ~1 ms
   means the ranking service is out of capacity, not the model: one worker scores about
   80 requests/s, so raise its `WEB_CONCURRENCY` (with `RANKING_THREADS=1`) or add
   replicas.
3. Check Postgres (`pg_stat_activity`) and Redis latency; the serving path reads both.
4. CPU-bound API processes: raise `WEB_CONCURRENCY` (uvicorn workers per container) up to
   the cores it has, or add replicas. Keep `WEB_CONCURRENCY x PG_MAX_CONNECTIONS` plus the
   stream and worker pools under Postgres `max_connections`. With more than one worker,
   `PROMETHEUS_MULTIPROC_DIR` must be set or `/metrics` shows one worker's counts.

## rate-limits
`rate_limited_total{bucket}` counts customer requests refused with 429 (D-1). A steady
rise for many customers usually means an app retry loop: check the app release before
raising `RATE_LIMIT_*_PER_MINUTE`. Limits are per customer across all workers (Redis); if
Redis is down they are not applied, and serving continues.

## fallback-rate
More than 5% of responses are `FALLBACK`.
- `ranking_degraded_total{reason}` rising → ranking service problem; the guardrail should
  roll back on its own within `GUARDRAIL_INTERVAL_SECONDS`. If it did not, roll back by hand.
- Otherwise the feature store or catalog is failing (`recommend_safe` fallback): see
  [redis-loss](#redis-loss).

## tracing
Every error body, `x-correlation-id` header, log line and audit row carries a `traceId`:
with tracing on it is the OpenTelemetry trace id, so it opens the request in the tracing
UI (locally Jaeger, http://localhost:16686, or `GET /api/traces/<traceId>`). A client that
sends its own `x-correlation-id` keeps it; it is then the span attribute `correlation.id`.
- Kafka carries the trace in message headers (`traceparent`): an event's trace runs from
  its producer (locally the simulator, one trace per event) through the stream's
  processing, its Redis and Postgres calls, and on into the DLQ or `customer.features`.
  Events from a producer that does not trace start their own trace in the stream.
- On when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (api, ranking, stream). Production: sample with
  `OTEL_TRACES_SAMPLER=parentbased_traceidratio` and `OTEL_TRACES_SAMPLER_ARG=0.1`;
  tracing every request cost about 30% of API throughput on the load-test host.
- Spans carry SQL text and Redis command names only, never the values bound to them.
  With `REDIS_CLUSTER=true` the instrumentation's Redis spans are named `redis` and carry
  no command name: timing only.
  Paths include the customer id, as the access logs do.
- A trace store outage loses spans, not requests: export is batched and in the
  background.


5xx rate > 1%. Take a `traceId` from a failing response, open it in the tracing UI (see
[tracing](#tracing)) or grep the API logs for it; the
log line carries the exception. Postgres down is the usual cause of 5xx: serving itself
returns an empty FALLBACK list rather than erroring, but admin endpoints need Postgres.

## model-rollback
Automatic: the guardrail rolls back when the live arm's degraded rate exceeds
`GUARDRAIL_MAX_DEGRADED_RATE` (5%) or its p95 bucket exceeds `GUARDRAIL_MAX_P95_MS` (500 ms)
over at least `GUARDRAIL_MIN_REQUESTS`. It writes an audit row with actor
`system:guardrail`, outcome `AUTOMATIC`, and the breach reasons.

Manual (Approver or Platform Operator):
```bash
curl -X POST localhost:8000/admin/v1/models/<version>/rollback -H "Authorization: Bearer <token>"
```
Rollback goes to the previous model that served (CANARY/FULL), or to the baseline if there
is none; a model that was only ever in SHADOW is never restored. It never needs
approval; re-promotion always does. Cache is invalidated on every change.

`ARTIFACT_INTEGRITY` in `ranking_degraded` / a promote refused with it: the model file
on the volume no longer matches the SHA-256 recorded at training (T-4). Treat it as a
security incident: do not re-promote, keep the file for investigation, retrain.
`ARTIFACT_DIGEST_MISSING`: the model was trained before digests were recorded; retrain it.

## kafka-topics
Before go-live, and on any new cluster:
`python scripts/create_topics.py --bootstrap <brokers> --partitions 96 --replication-factor 3`
(ADR-0014). It creates what is missing and never alters an existing topic: it prints how
one differs and exits 1. Adding partitions to `cc.transactions` moves customers between
partitions, so do it only as a planned change with the stream drained. `--dry-run` shows
the plan.

## pgbouncer
Put PgBouncer (transaction pooling) between the consumers/API and Postgres once their pools
add up past `max_connections` (ADR-0014). PgBouncer 1.21+ with `max_prepared_statements`
above 0 needs nothing else; otherwise set `PG_STATEMENT_CACHE_SIZE=0`, or queries fail
with "prepared statement ... does not exist". Run the migration job against Postgres
directly: it holds a session-level advisory lock.

## stream-scaling
Consumers scale by partition within the `STREAM_GROUP_ID` group (ADR-0014): add consumer
processes up to the partition count; each takes a share of partitions on rebalance, and a
customer's events stay ordered. Watch consumer lag, not CPU. If adding consumers stops
helping, check Redis CPU first (`INFO commandstats`: `hgetall`): it was the limit at ~830
events/s on a single Redis. Several consumers on one host need distinct
`STREAM_METRICS_PORT`s. Measure with
`python scripts/loadtest.py kafka --topic <bench topic> --group <bench group> --copies 20
--distinct-customers` against consumers writing to a benchmark database, never the live
topic (the tool refuses it).

## quarantine
More than 5% of events quarantined. `GET /admin/v1/metrics/overview` →
`quarantineByReason`. Quarantined events are in `transaction_log` (outcome `QUARANTINED`)
and on `pipeline.dlq`. Fix the producer; do not hand-edit the log. Once fixed, re-send the
events with **the same eventId** only if they were never applied.
A message missing what `transaction_log` requires (event id, customer, time) is still
quarantined and logged: its event id becomes `invalid:<hash of the message>` and its time
the arrival time, so it cannot fail the batch and stall the partition. Messages that are
not a JSON object at all only count in `undecodable_messages`.

## auto-retrain
`AutoRetrainFailing`: an automatic check or training job failed. The worker log names the
step (`auto_retrain`, `ml_jobs`); `GET /admin/v1/training-jobs` shows the job and its
error. Common causes: the data volume is full (each run writes a `live-*` snapshot; retention
keeps the newest `AUTO_RETRAIN_KEEP_EXPORTS` plus those in use, audited as
`dataset.prune`), or the export found no observable impressions. Nothing serving changes:
the current model stays until someone promotes another.

`AutoTrainedModelInShadow`: an automatically trained model passed its gates and went to
SHADOW (actor `system:auto-retrain` in the audit log). An Approver reviews the shadow
numbers on the Models page before any CANARY. To take it out of SHADOW, roll back.
To stop the loop: request `auto_retrain_interval_hours = 0` (see learning-switches).

## worker
The worker (`python -m rec.worker`, service `worker`, ADR-0012) runs training jobs, the
scheduled uplift report, auto-retrain and the online bandit. If it is down (`TargetDown`
for job `worker`): serving is unaffected, but queued training jobs wait and nothing learns.
Restart or scale it (`docker compose up -d --scale worker=2`); jobs are claimed with SKIP
LOCKED and the other passes take Redis locks, so several workers are safe. A job left
RUNNING past `TRAINING_TIMEOUT_HOURS` (6) is failed by the next worker turn with "no worker
finished it within the training timeout"; submit it again.

## uplift-report
`UpliftReportFailing`: the worker's scheduled report (`UPLIFT_REPORT_INTERVAL_HOURS`, 24)
raised; the worker log (`uplift`) has the traceback. A report skipped for lack of data
(no holdout, fewer than 200 customers with a closed window) is counted as `insufficient`,
not failed. Run it by hand with `python -m rec.ml.uplift` inside the worker container.

## online-bandit
`BanditLearningFailing` or `BanditLearningStalled`: the online bandit (shadow only, never
served) cannot learn. Check the API log (`bandit`) and Redis: the model, watermark and
contexts live there (`bandit:*`). Losing them only restarts learning from scratch; there is
no customer impact. To stop it: request `online_bandit_enabled = false` (see
learning-switches).

## promo-holdout
`PromoHoldoutSampleRatioMismatch`: over the last day the share of new customers in the
HOLDOUT arm differs from `PROMO_HOLDOUT_PERCENT` by more than 3 points. Assignment is a
deterministic hash, so this means recording is losing rows for one arm, or the split was
changed mid-experiment (see `promo_experiment.holdout_percent`). An uplift estimate from a
mismatched experiment is not trustworthy: pause the analysis, find the cause, and note it
in the report. Turning the holdout off (`PROMO_HOLDOUT_PERCENT=0`) restores offers to all
customers at once (request it; see learning-switches).

## learning-switches
Change a learning setting on the dashboard (Pembelajaran → Pengaturan pembelajaran) or
`POST /admin/v1/learning/settings/requests`: an ML Engineer or Platform Operator files it
with a reason, an Approver decides. It applies to every replica within 15 s, no restart.
A request nobody decides within 7 days expires; file it again if it still stands.
After the first approval env no longer decides any switch (ADR-0011).

`LearningSwitchChanged`: a replica's value changed. Expected right after an approval (the
audit shows `config.learning` by the Approver, with the request). If there is no such
approval, a replica started with different env values before the first approval
(`system:config` row): confirm it was intended, then file it as a request so it is
approved. A holdout change during a running experiment invalidates it (see promo-holdout).

## redis-loss
Redis holds derived state only (SDD 3.3). Admin sessions, cache and features are lost;
serving falls back to the popular list meanwhile.
```bash
docker compose up -d redis
docker compose exec api python scripts/rebuild_state.py --flush
```
The rebuild replays every logged envelope in original order through the same processor and
re-applies erasure tombstones first. Tested: `test_redis_state_rebuilds_exactly_from_postgres`.
The same rebuild migrates Redis to a new key layout (ADR-0014 changed it: deploy, then
rebuild once with `--flush`). For Redis Cluster set `REDIS_CLUSTER=true` and point
`REDIS_URL` at any node; every per-customer key is hash-tagged `{customerId}`, so a
customer's state, ledger, cache and tombstone live in one slot.
Until the rebuild runs, the stream still refuses erased customers: an unknown customer id
is checked against the Postgres tombstones before anything is logged.

## kafka-outage
The simulator retries each send with backoff and, if the broker stays down, marks the run
`FAILED` **with the checkpoint on the unsent event** — resume it (`POST
/admin/v1/simulations/<id>/resume` or the Simulator page) once Kafka is back. Nothing is
skipped. The consumer commits offsets only after a batch is applied and logged, so a
consumer crash redelivers, and dedup makes redelivery a no-op. Drill: `scripts/chaos.sh`.

## backup-and-restore
```bash
scripts/backup.sh backup                 # -> backups/<UTC timestamp>/
scripts/backup.sh restore backups/<ts>   # verify checksums, restore, rebuild Redis
scripts/dr_drill.sh                      # full drill: backup, destroy, restore, verify
```
What is backed up: the Postgres dump (source of truth: master data, transaction log,
audit, models, deployment, erasure tombstones), model artifacts and MLflow. Redis is not —
it is rebuilt. Dataset files under `data/` are regenerable from their manifest seed.

**Restore resurrects erased customers if the backup predates the erasure.** After any
restore from an older backup, re-run the erasures recorded since (the `erasure.execute`
audit rows survive in newer backups; keep the erasure list off-site with the backups).
Production: managed Postgres PITR, backups encrypted at rest, retention per policy.

## erasure
AC-009, maker-checker: a Platform Operator files `POST /admin/v1/erasure-requests`
(customerId, reason); a different person with the Approver role decides via
`POST /admin/v1/erasure-requests/<id>/decision`. Approval deletes, in one Postgres
transaction, the customer row, transaction log, impressions, interactions, shadow rows and
promo-experiment arm, writes a tombstone, then drops Redis state, cache, served-slate
records and the online bandit's stored contexts. The stream drops any later event for
that customer — including invalid ones — without logging its envelope; master-data reload
and training both skip tombstoned ids.

Not covered: Kafka topics (bounded by topic retention), dataset files already on disk
(filtered on read, not rewritten; auto-retrain exports age out after
`AUTO_RETRAIN_KEEP_EXPORTS` runs unless a serving model depends on one), MLflow artifacts of models trained before the erasure
(retrain to purge), and audit rows (immutable; they carry only the pseudonymous id).
Legal must confirm this scope under UU PDP.

## secrets-and-rotation
Settings are read from env vars or from files in `/run/secrets/<setting_name>` (docker /
Kubernetes secrets, or a secret-manager CSI mount). Nothing secret is in the image.
- **OIDC client secret:** create a second secret in the IdP, deploy it as
  `oidc_client_secret`, restart the API, then revoke the old one. Sessions survive.
- **Postgres password:** add the new password on the role (`ALTER ROLE ... PASSWORD`),
  deploy the new DSN, restart services, then remove the old credential. Services use
  `rec_app`; only the migration job holds the owner's credential (see below).
- **Schema changes (E-4):** services run with `DB_AUTO_MIGRATE=false` as `rec_app`. Before
  a deploy, run `python -m rec.store.pg` once with the owner's `POSTGRES_DSN`: it applies
  `db/schema.sql` and re-grants `rec_app` data access to every table. The owner's
  credential never reaches a service. Provision `rec_app` yourself (LOGIN, own password);
  `APP_DB_PASSWORD` is for the local stack only.
- **Ranking service token** (`ranking_service_token`, D-5): the ranking service accepts
  exactly one value, so rotate with both down briefly or at a quiet moment: deploy the
  new value to the ranking service and the API together. A mismatch degrades serving to
  the baseline (`ranking_degraded_total{reason="RANKING_HTTP_401"}`), it does not fail it.
- **Session store:** there is no signing key — sessions are opaque random ids. To force
  everyone out, `redis-cli --scan --pattern 'sess:*' | xargs redis-cli del`.

## Local stack security gaps (production must close them)
TLS is off everywhere, Kafka has no SASL/ACLs, Redis has no AUTH, Kafka is single-node
(RF 1). Production requires TLS on external and internal hops, SASL/SCRAM or mTLS with
per-service ACLs on Kafka, Redis AUTH + TLS, RF 3 / min-ISR 2, and encryption at rest.
