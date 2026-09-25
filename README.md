# Credit Card Merchant Recommendation Engine

Implementation of `sdd_recomendation_engine.md`, **Fase 1–4**: synthetic data → Kafka
replay → streaming feature engine → candidate generation + promo eligibility → baseline
or XGBoost Learning-to-Rank → recommendation API → Vue 3 operations console, with
feedback collection, attribution, training, evaluation gates and shadow/canary rollout.

Stack deviation from the SDD is deliberate and documented in
[ADR-0001](docs/adr/0001-python-first-stack.md): the services are Python (FastAPI +
aiokafka) instead of Spring Boot + Kafka Streams. Kafka, the event envelope, the topic
names, the feature schema and the REST contract are as specified, so a service can be
re-implemented on the JVM behind the same contract.

## Run it

```bash
cp .env.example .env
docker compose up -d --build          # kafka, postgres, redis, api, stream, dashboard
```

| Surface   | URL                          | Notes                                |
|-----------|------------------------------|--------------------------------------|
| API       | http://localhost:8000        | OpenAPI at `/docs`, health at `/health` |
| Dashboard | http://localhost:5173        | log in with a demo token below       |
| Ranking   | http://localhost:8100        | batch inference, `/health` lists loaded models |
| MLflow    | http://localhost:5000        | runs, metrics, artifacts, model registry |

Demo tokens (see `ADMIN_TOKENS` in `.env`): `admin-token` (Platform Operator),
`analyst-token` (Analyst), `ops-token` (Marketing Operator), `auditor-token` (Auditor),
`ml-token` (ML Engineer), `approver-token` (Approver). A customer token is
`cust-<customerId>`. Roles matter: an ML Engineer can train but not promote, and an
Approver can promote but not train (SEC-001 separation of duties).

End-to-end proof on the running stack — generate, replay through Kafka, serve, reconcile:

```bash
bash scripts/smoke_e2e.sh     # data path: generate, replay, serve, reconcile
bash scripts/smoke_ml.sh      # ML path: train, gate, promote, serve, degrade, rollback
```

`smoke_e2e.sh` fails loudly if online aggregates diverge from an offline recomputation
(AC-008). `smoke_ml.sh` fails if a model is promoted without passing its gates, if the
wrong role can promote, or if killing the ranking service takes recommendations down
instead of degrading them to the baseline.

## Tests

```bash
uv venv && uv pip install -e ".[dev]"
docker compose up -d postgres redis
.venv/bin/pytest tests -q          # 36 tests: unit, generator, integration, E2E
.venv/bin/ruff check rec tests scripts
cd dashboard && npm ci && npx vitest run && npx vue-tsc --noEmit && npm run build
```

`tests/test_e2e.py` needs Postgres and Redis; the ports are remapped to **55432** and
**56379** to avoid clashing with anything already local.

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
contracts/         frozen openapi.json, asyncapi.yaml
scripts/           reconcile.py (AC-008), smoke_e2e.sh, export_openapi.py
docs/adr/          architecture decisions
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

## Not built (out of the agreed Fase 1–4 scope)

- **Fase 5 — hardening.** SSO/OIDC, TLS, Kafka SASL/ACLs, secret manager, key rotation,
  load and resilience tests, automated canary guardrail rollback, backup/restore runbooks.
- **AC-009 (data deletion)** — erasure across cache, feature store and archive is not
  implemented.
- **Live-feedback training.** Training reads a dataset directory, which is what makes it
  reproducible. The API records impressions and interactions into Postgres, but there is
  no exporter turning that live feedback into a training dataset yet.
- **Controlled experiments.** Attribution is last-touch for reporting. Causal uplift needs
  an A/B holdout, which the canary machinery could carry but does not measure.

## Known gaps to close before customer traffic

1. **Auth.** SDD 13.4 requires a BFF with an `HttpOnly` session cookie and CSRF
   protection. This build uses bearer tokens held in `sessionStorage`, and SSE passes the
   token as a query parameter because `EventSource` cannot set headers. Backend RBAC is
   real and enforced (`rec/api/auth.py`); the token transport is not production-grade.
2. **Ingestion throughput.** Measured ~120 events/s locally against the 10 000 TPS
   target. The ceiling is one Postgres INSERT per event into `transaction_log`
   (`rec/store/pg.py`); reaching the NFR needs a batched COPY sink or an async archive
   writer, plus a real load test.
3. **Exactly-once.** At-least-once with idempotent handling, per
   [ADR-0003](docs/adr/0003-at-least-once-with-idempotent-handling.md). Dedup is bounded
   by a 7-day TTL.
4. **Single-node local Kafka.** `replication.factor=1`; production needs RF 3 / min-ISR 2.
5. **Late-event policy.** Arrivals beyond 24 h are counted and applied, not routed to a
   separate correction/backfill path (FEAT-003).
6. **Ranking latency is unmeasured here.** In-process prediction over 200 candidates is
   ~0.7 ms p95. The same call through the containerised service on this host ranged from
   13 ms to 157 ms for identical input — host contention, not code. `nthread` is pinned to
   2 because XGBoost's default 16-thread pool costs more to start than it saves on 200
   rows, but the p95 <500 ms NFR needs a real load test on representative hardware before
   anyone should believe a number.
7. **Promo quota in training rows.** Historical eligibility uses current `quota_used`;
   point-in-time quota exhaustion is not reconstructable from the master table.

## Synthetic data is simulation, not evidence

Distribution parameters in the manifest are simulation assumptions. Pipeline behaviour on
this data proves integrity and reproducibility; it proves nothing about business uplift.
Running a second experiment requires `idNamespace` —
see [ADR-0004](docs/adr/0004-per-dataset-id-namespace.md).
