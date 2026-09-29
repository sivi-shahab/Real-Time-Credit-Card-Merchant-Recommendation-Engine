# Fase 5 exit gate — before real customer traffic

SDD 18 exit criterion: security, performance, recovery and operational gates are
**approved** before customer traffic. This page is the evidence pack; approval is a human
decision and the sign-off lines below stay blank until someone accountable fills them.

Status legend: **PASS** verified here · **PARTIAL** built and verified locally, a
production step remains · **OPEN** not met.

## Security

| Check | Status | Evidence |
|---|---|---|
| SSO via OIDC (code + PKCE), BFF session in `HttpOnly` cookie, no token in browser storage | PASS | `tests/test_security.py` (fake IdP); live login through Keycloak via the dashboard origin; ADR-0006 |
| CSRF on every cookie-authenticated mutation | PASS | `test_mutation_without_csrf_token_is_refused`; live check through nginx |
| SSE no longer takes a token in the URL | PASS | `test_sse_no_longer_accepts_a_token_in_the_url` |
| RBAC on every admin route, every role | PASS | `test_every_admin_route_is_guarded`, `test_rbac_matrix_every_role_every_route` (>80 role×route denials) |
| Separation of duties: one role per identity; promote ≠ train; erasure requester ≠ approver | PASS | `test_one_identity_one_role`, live refusal of a dual-role Keycloak user, AC-009 test |
| IDOR/BOLA (AC-006) | PASS | `test_ac006_customer_token_cannot_read_another_customer` |
| Injection | PASS | parameterised SQL throughout; `test_sql_metacharacters_are_data_not_code`; bandit clean at medium+ |
| Secret leakage in logs | PASS | JSON logs through `RedactingFilter` (bearer, cookies, DSN passwords, PAN-shaped digits); `test_logs_redact_credentials` |
| Static demo tokens dead outside dev | PASS | `test_static_tokens_are_dead_outside_dev` |
| Dependency / SAST / SBOM in CI | PARTIAL | bandit, pip-audit (0 known vulns today), CycloneDX SBOM, `npm audit` in CI. `npm audit` failed from the first commit until vite 8 / vitest 5 / echarts 6 (0 vulns now), so the frontend type-check, tests and build had not run in CI before; the dashboard image now builds from the lockfile (`npm ci`). Image signing and container scanning need the registry |
| TLS everywhere, Kafka SASL/ACLs, Redis AUTH, encryption at rest | OPEN | local stack is plaintext; required config listed in `docs/runbooks.md` |
| Threat model | PASS | [threat-model.md](threat-model.md): STRIDE per trust boundary, including the learning features (ADR-0007 to ADR-0010); three findings fixed with tests (forgeable customer tokens now fail closed outside dev, model-version path traversal, feedback writable by any staff role for any customer) |
| Feedback accepted only from the customer it describes (T-7) | PASS | `test_feedback_is_accepted_only_from_the_customer_it_describes` |
| Automatic learning cannot put a model in front of customers without an Approver (E-6) | PASS | `test_auto_trained_model_reaches_shadow_only_while_nothing_serves` (also audited as `system:auto-retrain`), `test_rollback_never_puts_a_shadow_model_on_full_traffic` |
| Bandit state is data, not code (T-9) | PARTIAL | JSON, not pickle: `test_state_round_trips_through_json_and_keeps_learning_identically`. Its integrity waits on Redis AUTH |
| Live feedback bound to served responses (S-5) | PARTIAL | impressions must match a slate served to that customer (merchant, position; served model recorded), clicks must match an own impression: `test_feedback_must_match_a_slate_that_was_served`. Clicks on items really shown cannot be proven; feedback is limited to 120 calls per customer a minute (D-1: `test_each_customer_is_rate_limited_on_its_own`) |
| Learning switches under change control (T-10, E-7) | PASS | maker-checker (ADR-0011): `test_learning_settings_change_needs_a_second_person`, `test_env_cannot_move_a_switch_past_an_approved_value`, `test_rejected_or_invalid_learning_changes_apply_nothing`; live request → approve through the dashboard. Go-live should include one approved change so env stops deciding |
| Customer channel authentication (threat S-2) | PARTIAL | OIDC access tokens verified against the customer IdP's JWKS (signature RS256/ES256, iss, aud, exp, nbf; ADR-0013): `test_only_a_valid_token_from_the_customer_idp_signs_a_customer_in`. To close: the mobile platform confirms the token profile and `CUSTOMER_JWKS_URL` / `_JWT_ISSUER` / `_JWT_AUDIENCE` are set |
| Model artifact integrity between approval and load (T-4) | PASS | SHA-256 recorded at training, verified by the ranking service on every load; mismatch or missing digest serves baseline: `test_artifact_replaced_after_training_is_refused` |
| Event contracts: Avro + `BACKWARD_TRANSITIVE` in CI (EVT-004) | PASS | `tests/test_contracts.py`, `scripts/check_avro_compat.py` |
| External penetration test; PCI DSS / UU PDP scoping (SEC-003) | OPEN | needs the authorised parties; nothing here claims compliance |

## Privacy

| Check | Status | Evidence |
|---|---|---|
| AC-009 erasure, maker-checker, not rematerialised by replay or reload | PASS | `test_ac009_erased_customer_is_not_rematerialised_by_replay` (includes invalid events for the erased id; also removes the promo-holdout arm, bandit contexts and served-slate records) |
| Erasure scope beyond Postgres/Redis | PARTIAL | Kafka retention, on-disk datasets (filtered on read) including auto-retrain `live-*` exports, old MLflow artifacts, immutable audit rows — legal to confirm (`docs/runbooks.md#erasure`) |
| Retention of auto-retrain exports (I-9) | PARTIAL | newest 3 kept plus those in use, deletions audited: `test_prune_keeps_newest_and_pinned_and_touches_nothing_else`, `test_export_retention_spares_the_dataset_behind_a_serving_model`. Legal to confirm that erased customers may remain in retained exports until they age out |
| Promo holdout withholds offers from real customers (ADR-0010) | OPEN | business and legal approval of size, duration and consumer-protection position before `PROMO_HOLDOUT_PERCENT > 0` |

## Audit

| Check | Status | Evidence |
|---|---|---|
| Admin actions, logins, logouts, denials, rejected promotions, automatic rollbacks audited | PASS | `test_denials_are_audited`, guardrail test, BFF audit calls |
| Audit append-only at the database | PASS | trigger; `test_audit_log_cannot_be_rewritten` |
| App connects as a non-owner role (so it cannot drop the trigger) | PASS | services connect as `rec_app` (DML only, audit insert-only); a separate owner job migrates: `test_the_app_role_has_data_access_only`, and `smoke_e2e.sh` / `smoke_ml.sh` pass with the whole stack on `rec_app`. Production provisions the role and runs the job before each deploy |

## Performance (SDD 15)

Measured on the development host (WSL2, all services and the load generator on one box).
Redis inside its own container benchmarks at ~10k GET/s here, so these numbers bound the
host, not the design. Raw results: `docs/perf/results.jsonl`.

| Target | Result | Status |
|---|---|---|
| API p95 < 200 ms | 32 concurrent clients, 8 uvicorn workers (`WEB_CONCURRENCY`), `ab` on a 16-core host: cache hits **p95 101–168 ms** (515–1520 rps), every request a cache miss (`refresh=true`) **p95 145–173 ms** (335–390 rps), 0 errors. One worker: p95 260–460 ms cached, 600–1100 ms uncached. The earlier "p95 1 s" was mostly the load generator: `scripts/loadtest.py api` is one Python process and saturates a core near 150 rps. A cache miss used to parse the whole merchant catalog and every promotion (and saw only the first 1000 merchants of 1170); they are now held per process, re-read when a trigger bumps `catalog_version`, with promotion windows still checked per request (AC-005): 52 → 14–24 ms CPU  Re-measured 2026-09-29 with the serving log (Superset) on, same method, a busier host: cache hits p95 133–152 ms with production tracing (5% sampled), 96–149 ms untraced, 179–194 ms tracing every request; the release-gate commit on the same host 83–147 ms. Cache misses 466–525 ms now vs 581–610 ms for the release-gate commit on that host, so the host, not the code | PASS |
| Ingestion 10 000 TPS steady / 20 000 burst | **~220 events/s** per consumer process on this host (`scripts/loadtest.py ingest`). The consumer is CPU-bound, not Redis-bound: 12–14 s of CPU in a 14–16 s run, Redis at <1% CPU, ~1 ms RTT. The cost is spread over asyncio and the redis-py client, with no single hotspot; hiredis and uvloop each gave <10%. Redis round trips per event cut from 5 to 2 (one pipeline to admit, one transaction to save and invalidate), +10–15% here, more where Redis is across a network. Through the broker (`loadtest.py kafka`, 12 partitions): 1 consumer 278/s, 4 → 687/s, 8 → 804–837/s, then flat: Redis saturates, half of it on the per-event `HGETALL` of the whole customer state, half of which is the 180-day transaction ledger. [ADR-0014](adr/0014-ingestion-scale-out.md): built — ledger out of the per-event read (`HMGET` of the named ids over monthly hashes), per-customer hash tags and tombstones, Redis Cluster client (whole suite passes on a 3-primary cluster), one feature update per customer per batch; measured after: 8 consumers 837 → 1 070/s with Redis CPU down from ~125% to ~51%. Platform to size: 96 partitions (`scripts/create_topics.py`), a consumer per partition at peak, Redis Cluster primaries, PgBouncer (`PG_STATEMENT_CACHE_SIZE=0` if it lacks prepared statements; verified against PgBouncer 1.25) | OPEN |
| On-demand ranking p95 < 500 ms | Model serving (FULL), every request a cache miss, 32 clients, API 8 workers and ranking 8 single-threaded workers on one 16-core host: **p95 376–419 ms** at 166–208 rps, 0.5% degraded by timeout, the guardrail kept the model live. Before: building a DMatrix per request (a thread per core, 67 ms) and 235 KB of JSON per call made the guardrail roll the model back within seconds; now `inplace_predict` on a float32 matrix sent as base64 (predict ~1 ms, `test_the_client_and_the_service_agree_on_every_score`). Measure through the container network, not a WSL published port: docker-proxy capped the ranking service at ~200 rps where it serves ~930 | PASS |

What changed in Fase 5: transaction log batched per consumer batch; cache-hit path no
longer parses every active promotion; asyncpg reset round trip removed; responses
serialised by pydantic-core (API in-process cost 22 ms → 12 ms per request).
To close: run on representative hardware with one consumer per partition, a real Kafka
load generator, and an HTTP load generator that is not itself the bottleneck (`ab`, k6).

## Recovery

| Check | Status | Evidence |
|---|---|---|
| Restore practised | PASS | `scripts/dr_drill.sh` on the live stack: backup → drop schema + flush Redis → restore → rebuild; row counts and 25 busiest customers' features identical; restore+rebuild 146 s for 16.8k events |
| Replay / rebuild state from Postgres | PASS | `test_redis_state_rebuilds_exactly_from_postgres`; `scripts/rebuild_state.py` |
| Rollback practised — manual and automatic | PASS | `smoke_ml.sh`; live guardrail: ranking killed with a FULL model → 60/60 responses HTTP 200, automatic rollback to baseline in 4 s, audited |
| Consumer crash + broker loss | PASS | `scripts/chaos.sh`: consumer SIGKILL mid-replay + Kafka stop → replay completed, 0/148 reconciliation mismatches |
| Simulator never skips on broker loss | PASS | `test_simulator_never_skips_an_event_when_the_broker_fails` (was: failed sends were silently dropped) |
| Redis loss | PASS | `test_redis_loss_degrades_to_popular_not_to_an_error`; chaos step 1 live |
| PITR, cross-zone replicas, restore RTO/RPO targets agreed | OPEN | production platform |

## Operations

| Check | Status | Evidence |
|---|---|---|
| Metrics | PASS | `/metrics` on api, ranking, stream; Prometheus scraping all three |
| Alerts with runbook links | PASS | `deploy/prometheus/alerts.yml` (14 rules) → `docs/runbooks.md`; `promtool check` and the rule unit tests in `alerts_test.yml` run in CI |
| Trace correlation | PASS | OpenTelemetry across api → ranking (W3C `traceparent`) and across Kafka in message headers, simulator → stream → DLQ / `customer.features` (`test_a_trace_crosses_kafka_from_the_simulator_into_the_stream`), with Postgres and Redis spans; the `traceId` in every error, header, log line and audit row is the trace id: `test_one_trace_spans_api_and_ranking_and_carries_no_values`. Spans hold SQL and command names, never bound values. 10% sampling costs nothing measurable here; 100% (the local stack) about 30% of throughput |
| Runbooks | PASS | `docs/runbooks.md` |
| Alert routing (pager), on-call rota, dashboards in the ops tool | OPEN | organisational |
| Learning loops observable (auto-retrain runs, bandit learning, holdout exposure) | PASS | metrics for each loop, asserted in the e2e tests that drive them; alerts for failures, stalls, automatic SHADOW, holdout sample-ratio mismatch and setting changes, each with a unit test and a runbook section; state on the Pembelajaran page |
| Training isolated from serving (D-7) | PASS | worker process (ADR-0012): `test_training_is_queued_for_a_worker_and_claimed_once`; live: a job submitted to the API ran and was counted only in the worker. Dataset generation still runs in the API |
| Scheduled uplift report | PASS | worker, every 24 h, locked and alerted (`UpliftReportFailing`): `test_scheduled_uplift_report_saves_what_it_estimates`, `test_scheduled_uplift_report_is_locked_and_skips_without_data` |

## Sign-off

| Gate | Name | Decision | Date |
|---|---|---|---|
| Security | | | |
| Privacy / Legal | | | |
| Performance | | | |
| Recovery | | | |
| Operations | | | |
