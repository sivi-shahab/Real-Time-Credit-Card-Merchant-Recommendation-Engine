# Credit Card Merchant Recommendation Engine

Implementation of `sdd_recomendation_engine.md`, **Fase 1–3**: synthetic data → Kafka
replay → streaming feature engine → candidate generation + promo eligibility + baseline
ranking → recommendation API → Vue 3 operations console.

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

Demo tokens (see `ADMIN_TOKENS` in `.env`): `admin-token` (Platform Operator),
`analyst-token` (Analyst), `ops-token` (Marketing Operator), `auditor-token` (Auditor).
A customer token is `cust-<customerId>`.

End-to-end proof on the running stack — generate, replay through Kafka, serve, reconcile:

```bash
bash scripts/smoke_e2e.sh
```

It fails loudly if online aggregates diverge from an offline recomputation (AC-008).

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
rec/core/          pure domain: money, ledger, features, eligibility, ranking  (no IO)
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

SYN-006 reproducibility, SYN-005 fault handling, RBAC, optimistic concurrency, audit
logging, cursor pagination and envelope redaction have their own tests in the same suite.

## Not built (out of the agreed Fase 1–3 scope)

- **Fase 4 — ML.** No XGBoost `rank:ndcg` model, MLflow registry, training pipeline,
  attribution windows, or model promote/rollback. Baseline ranking only (ML-001). The
  feedback endpoints record impressions and interactions, but nothing trains on them yet.
- **Fase 5 — hardening.** SSO/OIDC, TLS, Kafka SASL/ACLs, secret manager, key rotation,
  load and resilience tests, canary/rollback, backup/restore runbooks.
- **AC-009 (data deletion)** — erasure across cache, feature store and archive is not
  implemented.

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

## Synthetic data is simulation, not evidence

Distribution parameters in the manifest are simulation assumptions. Pipeline behaviour on
this data proves integrity and reproducibility; it proves nothing about business uplift.
Running a second experiment requires `idNamespace` —
see [ADR-0004](docs/adr/0004-per-dataset-id-namespace.md).
