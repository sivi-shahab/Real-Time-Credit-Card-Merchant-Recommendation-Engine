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
3. Check Postgres (`pg_stat_activity`) and Redis latency; the serving path reads both.

## fallback-rate
More than 5% of responses are `FALLBACK`.
- `ranking_degraded_total{reason}` rising → ranking service problem; the guardrail should
  roll back on its own within `GUARDRAIL_INTERVAL_SECONDS`. If it did not, roll back by hand.
- Otherwise the feature store or catalog is failing (`recommend_safe` fallback): see
  [redis-loss](#redis-loss).

## api-errors
5xx rate > 1%. Take a `traceId` from a failing response and grep the API logs for it; the
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

## quarantine
More than 5% of events quarantined. `GET /admin/v1/metrics/overview` →
`quarantineByReason`. Quarantined events are in `transaction_log` (outcome `QUARANTINED`)
and on `pipeline.dlq`. Fix the producer; do not hand-edit the log. Once fixed, re-send the
events with **the same eventId** only if they were never applied.

## auto-retrain
`AutoRetrainFailing`: an automatic check or training job failed. The API log names the
step (`auto_retrain`, `ml_jobs`); `GET /admin/v1/training-jobs` shows the job and its
error. Common causes: the data volume is full (each run writes a `live-*` snapshot; retention
keeps the newest `AUTO_RETRAIN_KEEP_EXPORTS` plus those in use, audited as
`dataset.prune`), or the export found no observable impressions. Nothing serving changes:
the current model stays until someone promotes another.

`AutoTrainedModelInShadow`: an automatically trained model passed its gates and went to
SHADOW (actor `system:auto-retrain` in the audit log). An Approver reviews the shadow
numbers on the Models page before any CANARY. To take it out of SHADOW, roll back.
To stop the loop: `AUTO_RETRAIN_INTERVAL_HOURS=0` and restart the API.

## online-bandit
`BanditLearningFailing` or `BanditLearningStalled`: the online bandit (shadow only, never
served) cannot learn. Check the API log (`bandit`) and Redis: the model, watermark and
contexts live there (`bandit:*`). Losing them only restarts learning from scratch; there is
no customer impact. To stop it: `ONLINE_BANDIT_ENABLED=false` and restart the API.

## promo-holdout
`PromoHoldoutSampleRatioMismatch`: over the last day the share of new customers in the
HOLDOUT arm differs from `PROMO_HOLDOUT_PERCENT` by more than 3 points. Assignment is a
deterministic hash, so this means recording is losing rows for one arm, or the split was
changed mid-experiment (see `promo_experiment.holdout_percent`). An uplift estimate from a
mismatched experiment is not trustworthy: pause the analysis, find the cause, and note it
in the report. Turning the holdout off (`PROMO_HOLDOUT_PERCENT=0`) restores offers to all
customers at once.

## learning-switches
`LearningSwitchChanged`: a replica restarted with a different learning setting
(`auto_retrain_*`, `online_bandit_*`, `promo_holdout_percent`). The first replica to start
with new values writes an audit row (`system:config`, action `config.learning`, before and
after). Confirm the change was intended and approved; a holdout change during a running
experiment invalidates it (see promo-holdout).

## redis-loss
Redis holds derived state only (SDD 3.3). Admin sessions, cache and features are lost;
serving falls back to the popular list meanwhile.
```bash
docker compose up -d redis
docker compose exec api python scripts/rebuild_state.py --flush
```
The rebuild replays every logged envelope in original order through the same processor and
re-applies erasure tombstones first. Tested: `test_redis_state_rebuilds_exactly_from_postgres`.
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
  deploy the new DSN, restart services, then remove the old credential. Keep the app on a
  non-owner role in production, so it cannot drop the audit trigger.
- **Session store:** there is no signing key — sessions are opaque random ids. To force
  everyone out, `redis-cli --scan --pattern 'sess:*' | xargs redis-cli del`.

## Local stack security gaps (production must close them)
TLS is off everywhere, Kafka has no SASL/ACLs, Redis has no AUTH, Kafka is single-node
(RF 1). Production requires TLS on external and internal hops, SASL/SCRAM or mTLS with
per-service ACLs on Kafka, Redis AUTH + TLS, RF 3 / min-ISR 2, and encryption at rest.
