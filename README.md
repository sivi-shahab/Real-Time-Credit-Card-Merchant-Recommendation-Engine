# Credit Card Merchant Recommendation Engine

Implementation of `sdd_recomendation_engine.md`, **Fase 1–5**: synthetic data → Kafka
replay → streaming feature engine → candidate generation + promo eligibility → baseline
or XGBoost Learning-to-Rank → recommendation API → Vue 3 operations console, with
feedback collection, attribution, training, evaluation gates and shadow/canary rollout,
hardened with SSO, audit, observability, automatic rollback, erasure and practised
recovery. **Fase 5 is built; its exit gate is not yet approved** — see
[docs/release-gate.md](docs/release-gate.md) for what passed, what is open, and the
sign-off table.

Stack deviation from the SDD is deliberate and documented in
[ADR-0001](docs/adr/0001-python-first-stack.md): the services are Python (FastAPI +
aiokafka) instead of Spring Boot + Kafka Streams. Kafka, the event envelope, the topic
names, the feature schema and the REST contract are as specified, so a service can be
re-implemented on the JVM behind the same contract.

![Architecture](docs/architecture/rec-engine.svg)

Interactive version (zoom, search, guided views):
[sivi-shahab.github.io/…/architecture/rec-engine.html](https://sivi-shahab.github.io/Real-Time-Credit-Card-Merchant-Recommendation-Engine/architecture/rec-engine.html),
served by GitHub Pages from `docs/` on `main`. Both are generated
with archify from
[rec-engine.architecture.json](docs/architecture/rec-engine.architecture.json); after
editing the spec, re-run `deliver` and re-export the SVG from the viewer's Export menu.

<details>
<summary><b>Data ingestion in detail</b>: guard chain, ledger, quarantine, outputs</summary>

![Data ingestion pipeline](docs/architecture/data-ingestion.svg)

[Interactive](https://sivi-shahab.github.io/Real-Time-Credit-Card-Merchant-Recommendation-Engine/architecture/data-ingestion.html)
· spec: [data-ingestion.dataflow.json](docs/architecture/data-ingestion.dataflow.json)
</details>

<details>
<summary><b>MLOps in detail</b>: train, gate, promote, guardrail rollback</summary>

![MLOps workflow](docs/architecture/mlops.svg)

[Interactive](https://sivi-shahab.github.io/Real-Time-Credit-Card-Merchant-Recommendation-Engine/architecture/mlops.html)
· spec: [mlops.workflow.json](docs/architecture/mlops.workflow.json)
</details>

<details>
<summary><b>Recommendation request</b>: sequence from token check to cached response</summary>

![Recommendation request sequence](docs/architecture/recommendation-request.svg)

[Interactive](https://sivi-shahab.github.io/Real-Time-Credit-Card-Merchant-Recommendation-Engine/architecture/recommendation-request.html)
· spec: [recommendation-request.sequence.json](docs/architecture/recommendation-request.sequence.json)
</details>

## Run it

```bash
cp .env.example .env
docker compose up -d --build   # kafka, postgres, redis, api, stream, ranking, mlflow,
                               # dashboard, keycloak, prometheus
```

| Surface   | URL                          | Notes                                |
|-----------|------------------------------|--------------------------------------|
| API       | http://localhost:8000        | OpenAPI at `/docs`, health at `/health` |
| Dashboard | http://localhost:5173        | "Masuk dengan SSO", or a demo token (local only) |
| Keycloak  | http://localhost:8180        | realm `rec`; user `<name>` / `<name>-local-pass` |
| Prometheus| http://localhost:9090        | scrapes api, ranking, stream; alert rules loaded |
| Ranking   | http://localhost:8100        | batch inference, `/health` lists loaded models |
| MLflow    | http://localhost:5000        | runs, metrics, artifacts, model registry |

**Signing in.** The browser only ever holds an opaque `HttpOnly` session cookie issued by
the BFF (`/bff/*`, [ADR-0006](docs/adr/0006-bff-session-and-oidc.md)); mutations carry a
CSRF token. SSO users (Keycloak, one per role): `viewer`, `analyst`, `marketing`,
`mlengineer`, `operator`, `approver`, `auditor` — password `<user>-local-pass`. User
`conflicted` holds ML Engineer + Approver and is refused at login by design.

Demo bearer tokens work only when `ENVIRONMENT` is local/test/ci, for scripts and the
local "demo token" login: `admin-token` (Platform Operator), `analyst-token`,
`ops-token` (Marketing Operator), `auditor-token`, `ml-token` (ML Engineer),
`approver-token`, `viewer-token`. A customer token is `cust-<customerId>`. Roles matter:
an ML Engineer can train but not promote, an Approver can promote but not train, and an
erasure is filed by one person and approved by another (SEC-001).

End-to-end proof on the running stack — generate, replay through Kafka, serve, reconcile:

```bash
bash scripts/smoke_e2e.sh     # data path: generate, replay, serve, reconcile
bash scripts/smoke_ml.sh      # ML path: train, gate, promote, serve, degrade, rollback
bash scripts/chaos.sh         # Redis loss, ranking loss, consumer SIGKILL + broker loss
bash scripts/dr_drill.sh      # backup, destroy, restore, rebuild Redis, verify identical
python scripts/loadtest.py api --concurrency 8 --seconds 30   # latency/throughput
```
The scripts call `python3`; run them with the venv active (or `.venv/bin` on `PATH`).

`smoke_e2e.sh` fails loudly if online aggregates diverge from an offline recomputation
(AC-008). `smoke_ml.sh` fails if a model is promoted without passing its gates, if the
wrong role can promote, or if killing the ranking service takes recommendations down
instead of degrading them to the baseline.

## Tests

```bash
uv venv && uv pip install -e ".[dev]"
docker compose up -d postgres redis
.venv/bin/pytest tests -q          # 95 tests: unit, generator, ML, contract, security, E2E
.venv/bin/ruff check rec tests scripts
cd dashboard && npm ci && npx vitest run && npx vue-tsc --noEmit && npm run build
```

`tests/test_e2e.py` and `tests/test_security.py` need Postgres and Redis; the ports are
remapped to **55432** and **56379**. Tests use their own database (`rec_test`, created on
first run) and Redis DB 1, because the E2E fixture truncates tables — it must never touch
the running stack's data.

## Layout

```
rec/core/          pure domain: money, ledger, features, eligibility, baseline ranking (no IO)
rec/ml/            vectoriser, attribution, dataset builder, training, metrics, registry
rec/ranking/       Ranking Service: FastAPI + XGBoost batch inference
rec/generator/     SYN-001..006 reproducible dataset generation
rec/stream/        feature engine consumer: validate, dedup, aggregate, quarantine
rec/api/           recommendation API, admin BFF, dataset jobs, RBAC
rec/simulator/     SIM-001..003 replay with rate control and checkpoints
rec/store/         Redis online store, Postgres master data
dashboard/         Vue 3 + TS console (10 views)
contracts/         frozen openapi.json, asyncapi.yaml, avro/*.avsc (event contracts)
rec/obs.py         JSON logs with trace ids + redaction, Prometheus metrics
scripts/           reconcile.py (AC-008), smoke/chaos/DR drills, backup.sh,
                   rebuild_state.py, loadtest.py, export_openapi.py
deploy/            Keycloak realm, Prometheus scrape config + alert rules
docs/adr/          architecture decisions
docs/architecture/ archify diagrams (system, ingestion, MLOps, request sequence): spec, SVG, HTML
docs/runbooks.md   one section per alert, plus restore, erasure, key rotation
docs/release-gate.md  Fase 5 evidence pack and sign-off
docs/threat-model.md  STRIDE per trust boundary, with evidence and open risks
```

## Which acceptance criteria are covered

| ID     | Requirement                            | Verified by |
|--------|----------------------------------------|-------------|
| AC-001 | Redelivery changes no aggregate        | `test_ac001_duplicate_event_and_transaction_are_noops`, `test_ac001_full_replay_is_idempotent` |
| AC-002 | Partial refund cuts money, not count   | `test_ac002_partial_refund_cuts_monetary_not_frequency` |
| AC-003 | Window expiry with no new events       | `test_ac003_window_expiry_without_new_events` |
| AC-004 | Fallback on ranking failure            | `test_ac004_fallback_when_ranking_dependency_fails` |
| AC-005 | Expired promo in cache is not served   | `test_ac005_expired_promo_in_cache_is_not_served` |
| AC-006 | Customer token cannot read another     | `test_ac006_customer_token_cannot_read_another_customer` |
| AC-007 | Preview writes no impression, no cache | `test_ac007_preview_leaves_no_impression_and_no_cache` |
| AC-008 | Online matches offline recomputation   | `test_ac008_online_matches_offline_recomputation`, `scripts/reconcile.py` |

Fase 4 additions:

| Requirement | Verified by |
|-------------|-------------|
| ML-003 label precedence, observation window, no unexposed negatives | `test_label_precedence_takes_the_highest_valid_outcome`, `test_impression_without_interaction_is_zero_only_after_the_window_closes`, `test_label_from_events_withholds_unobservable_impressions` |
| ML-004 attribution window and single credited exposure | `test_outcome_outside_the_attribution_window_earns_no_credit`, `test_conversion_gets_exactly_one_credited_exposure` |
| ML-005 temporal split with observation gap, intact groups | `test_temporal_split_respects_the_observation_gap`, `test_groups_are_intact_never_split_across_sides` |
| No time leakage in the training set | `test_no_future_transaction_can_reach_a_past_training_row` |
| Training/serving feature parity | `test_serving_and_training_produce_identical_vectors`, `test_baseline_score_matches_the_production_formula` |
| Training reproducibility | `test_training_is_reproducible_and_reports_its_gates` |
| ML-006 gates block a bad model | `test_gates_block_a_model_that_loses_to_baseline_or_blows_the_budget`, `test_promotion_requires_approver_and_passing_gates`, `test_feature_schema_mismatch_blocks_promotion` |
| Model ranks live, degrades to baseline on failure | `test_model_ranks_when_promoted_and_degrades_when_it_fails` |
| Shadow does not change serving | `test_shadow_mode_does_not_change_what_is_served` |
| Canary share and per-customer stability | `test_canary_routes_only_its_share_and_is_stable_per_customer`, `test_canary_split_is_deterministic_and_roughly_proportional` |
| Rollback path and audit | `test_rollback_returns_to_previous_then_to_baseline`, `test_model_endpoints_are_audited` |

Fase 5 additions:

| Requirement | Verified by |
|-------------|-------------|
| AC-009 erasure: maker-checker, nothing rematerialised by replay or reload | `test_ac009_erased_customer_is_not_rematerialised_by_replay` |
| SDD 13.4 BFF: HttpOnly session, CSRF, server-side logout, no URL token | `test_session_cookie_is_httponly_and_carries_no_token`, `test_mutation_without_csrf_token_is_refused`, `test_logout_kills_the_session_server_side`, `test_sse_no_longer_accepts_a_token_in_the_url` |
| OIDC code + PKCE, nonce/audience checks, one role per identity | `test_oidc_code_flow_creates_a_session`, `test_oidc_rejects_wrong_nonce_audience_or_role_set`, `test_one_identity_one_role` |
| RBAC on every admin route × role; denials audited | `test_every_admin_route_is_guarded`, `test_rbac_matrix_every_role_every_route`, `test_denials_are_audited` |
| Audit cannot be rewritten | `test_audit_log_cannot_be_rewritten` |
| Automatic canary/full rollback on guardrail breach | `test_guardrail_rolls_back_a_failing_live_model`, `test_guardrail_p95_is_a_conservative_bucket_bound` |
| Resilience: Redis loss, broker loss | `test_redis_loss_degrades_to_popular_not_to_an_error`, `test_simulator_never_skips_an_event_when_the_broker_fails`, `scripts/chaos.sh` |
| Recovery: rebuild Redis from Postgres exactly | `test_redis_state_rebuilds_exactly_from_postgres`, `scripts/dr_drill.sh` |
| Log redaction, injection | `test_logs_redact_credentials`, `test_sql_metacharacters_are_data_not_code` |

Fase 0 completion (Avro contracts, threat model):

| Requirement | Verified by |
|-------------|-------------|
| Every produced Kafka message fits its Avro contract (EVT-001) | `tests/test_contracts.py` — generator events, feature updates and DLQ records round-tripped through Avro binary |
| EVT-004 `BACKWARD_TRANSITIVE` in CI | `scripts/check_avro_compat.py` against every committed version; `test_compatibility_check_rejects_breaking_changes` |
| Threat model findings fixed | `test_static_tokens_are_dead_outside_dev` (forgeable `cust-` tokens fail closed outside dev), `test_model_version_cannot_escape_the_model_directory`, `test_errors_are_uniform_and_requests_are_bounded` |

SYN-006 reproducibility, SYN-005 fault handling, RBAC, optimistic concurrency, audit
logging, cursor pagination and envelope redaction have their own tests in the same suite.

## The ML path

```
feedback events -> labels (ML-003) -> exposures + attribution (ML-004)
               -> point-in-time feature join on an event-time timeline (ML-005)
               -> temporal split with an observation-window gap
               -> XGBoost rank:ndcg  vs  the live baseline formula
               -> evaluation gates (ML-006) -> MLflow run + registry
               -> Approver promotes: SHADOW | CANARY n% | FULL
               -> serving calls the Ranking Service; any failure falls back to baseline
```

Measured on a 400-customer / 8.5k-transaction dataset (8 000 request groups, 64 000
exposures): NDCG@10 **0.4944** vs baseline **0.4744**, NDCG@5 **0.4152** vs **0.3833**,
all six gates passing. That demonstrates the pipeline is sound and the model learns
something the baseline does not — on synthetic data it says nothing about business uplift.

**Read @5, not @10, on this dataset.** The generator exposes ~8 items per request, so a
top-10 cut keeps the whole slate: `recall@10` is trivially 1.0 and coverage/diversity @10
are identical for every ranker. The training metadata carries this caveat explicitly
(`lineage.metricCaveat`) and the dashboard renders it.

## Not built

- **Production platform controls.** TLS on every hop, Kafka SASL/ACLs, Redis AUTH,
  encryption at rest, image signing, a non-owner DB role, PITR. The local stack is
  plaintext; `docs/runbooks.md` lists what production must set.
- **Machine-to-machine admin credentials.** Scripts use static tokens, which only work in
  local/test/ci. Production automation needs OIDC client credentials.
- **Distributed tracing.** A `traceId` joins errors, logs and audit rows, but there is no
  OpenTelemetry span propagation across api → ranking.
- **Live-feedback training.** Training reads a dataset directory, which is what makes it
  reproducible. The API records impressions and interactions into Postgres, but there is
  no exporter turning that live feedback into a training dataset yet.
- **Controlled experiments.** Attribution is last-touch for reporting. Causal uplift needs
  an A/B holdout, which the canary machinery could carry but does not measure.

## Known gaps to close before customer traffic

The full list with evidence and sign-off is [docs/release-gate.md](docs/release-gate.md);
threats are ranked in [docs/threat-model.md](docs/threat-model.md). The ones that matter
most:

0. **The customer API has no production authentication.** `cust-<id>` tokens are a
   forgeable stand-in and are refused outside local/test/ci; verifying the mobile
   channel's real tokens is not built (threat S-2).
1. **Ingestion throughput is unproven.** ~210 events/s per consumer on this host against
   the 10 000 TPS target. Fase 5 removed the Postgres ceiling (one fsync per event → one
   transaction per consumer batch); what remains is ~6 Redis round trips per event, on a
   host where Redis itself benchmarks at ~10k ops/s. The design scales by partition (6)
   and consumer replicas; that needs a load test on representative hardware.
2. **API latency holds only at low concurrency here.** 8 clients: p95 72 ms at 162 rps.
   32 clients: p95 ~1 s — one uvicorn worker plus the load generator on the same WSL host.
   The customer API should run as its own horizontally scaled deployment; the admin API
   cannot simply add workers because the simulator keeps run state in-process.
3. **id_token signature is not verified.** Safe only because it comes straight from the
   token endpoint over the back channel, and only if that channel is TLS in production
   (ADR-0006).
4. **Erasure scope.** Kafka retention, dataset files on disk (filtered on read, not
   rewritten), MLflow artifacts of older models and immutable audit rows are outside the
   automated erasure; a restore from a backup older than an erasure brings the customer
   back until the erasure is re-run (`docs/runbooks.md#erasure`).
5. **Exactly-once.** At-least-once with idempotent handling, per
   [ADR-0003](docs/adr/0003-at-least-once-with-idempotent-handling.md). Dedup is bounded
   by a 7-day TTL. With batched logging, a crash between the Redis write and the log flush
   re-logs those events as `DUPLICATE_EVENT`; features stay exact.
6. **Single-node local Kafka.** `replication.factor=1`; production needs RF 3 / min-ISR 2.
7. **Late-event policy.** Arrivals beyond 24 h are counted and applied, not routed to a
   separate correction/backfill path (FEAT-003).
8. **Promo quota in training rows.** Historical eligibility uses current `quota_used`;
   point-in-time quota exhaustion is not reconstructable from the master table.

## Synthetic data is simulation, not evidence

Distribution parameters in the manifest are simulation assumptions. Pipeline behaviour on
this data proves integrity and reproducibility; it proves nothing about business uplift.
Running a second experiment requires `idNamespace` —
see [ADR-0004](docs/adr/0004-per-dataset-id-namespace.md).
