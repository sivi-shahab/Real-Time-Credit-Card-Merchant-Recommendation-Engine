# Threat model

**Scope:** the system as built in this repository (Fase 1–5), STRIDE per trust boundary.
**Method:** each threat has an ID, the control that addresses it, the evidence (a test or
drill) and a status. **Mitigated** = control built and tested here; **Partial** = built,
but a production step remains; **Open** = not addressed yet. Open and Partial items are
also tracked in [release-gate.md](release-gate.md).

Update this document whenever a trust boundary, an external contract, or an auth
mechanism changes. The release-gate security sign-off covers it.

## What is being protected

| Asset | Why it matters |
|---|---|
| Customer transaction behaviour and derived features | Personal data under UU PDP, even with pseudonymous ids |
| Recommendation and promo-eligibility integrity | Wrong eligibility is a financial and reputational cost; a manipulated ranking is a mis-selling risk |
| Model artifacts and the deployment decision | Whoever controls these controls what every customer sees |
| Audit trail | Evidence for regulators and incident response |
| Sessions, upstream IdP tokens, service credentials | Every other control depends on them |

Out of scope by SDD 1.2: payments, PAN/CVV (never stored, SYN-002), credit scoring,
fraud detection.

## Trust boundaries

```
 B1  mobile channel ──► Customer API (/api/v1)                     internet-facing
 B2  staff browser ──► dashboard + BFF (/bff, /admin/v1) ◄──► IdP   internal users
 B3  transaction source / simulator ──► Kafka ──► feature engine   event ingress
 B4  services ──► Postgres · Redis · Kafka · model volume          data stores
 B5  API ──► ranking service;  training ──► model artifacts/MLflow ML supply chain
 B6  CI / operators ──► images, secrets, deployment                delivery
```

## Spoofing

| ID | Threat | Boundary | Control | Evidence | Status |
|---|---|---|---|---|---|
| S-1 | Staff impersonation, stolen or replayed session | B2 | OIDC code + PKCE; opaque `HttpOnly` cookie; 30 min idle / 8 h absolute; logout deletes the session server-side; static tokens only in local/test/ci | `test_session_cookie_is_httponly_and_carries_no_token`, `test_logout_kills_the_session_server_side`, `test_static_tokens_are_dead_outside_dev` | Mitigated |
| S-2 | Forged customer identity: `cust-<id>` is guessable | B1 | Fails closed outside dev environments. Real channel token verification (the mobile platform's JWT/mTLS) is **not built**, so the customer API has no production auth yet | `test_static_tokens_are_dead_outside_dev` | Open |
| S-3 | Anyone who can reach Kafka publishes fake transactions | B3 | Schema + reference validation and quarantine limit the damage, but they do not authenticate the producer. Needs SASL/mTLS and per-topic ACLs (produce on `cc.transactions` = ingestion only) | `test_pipeline_applies_and_quarantines_per_spec` | Open |
| S-4 | Substituted or replayed id_token at login | B2 | Single-use state (10 min), nonce, issuer, audience, expiry checked. Signature not verified — relies on the TLS back channel (ADR-0006) | `test_oidc_rejects_wrong_nonce_audience_or_role_set` | Partial |

## Tampering

| ID | Threat | Boundary | Control | Evidence | Status |
|---|---|---|---|---|---|
| T-1 | Audit rows edited or deleted | B4 | `BEFORE UPDATE OR DELETE` trigger; app role must not own the table (else it can drop the trigger) | `test_audit_log_cannot_be_rewritten` | Partial |
| T-2 | Cross-site request makes a signed-in operator change a promo or promote a model | B2 | Synchroniser CSRF token on every cookie-authenticated mutation; `SameSite=Lax` | `test_mutation_without_csrf_token_is_refused` | Mitigated |
| T-3 | Crafted `modelVersion` loads a file outside the model directory | B5 | Version must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` before it becomes a path | `test_model_version_cannot_escape_the_model_directory` | Mitigated |
| T-4 | Model artifact replaced on the shared volume after approval | B5 | Promotion requires passing gates, matching feature schema, an Approver and a successful warm. The file itself is not hashed: record the artifact SHA-256 at training and verify it at load | — | Open |
| T-5 | Redelivered or duplicated events inflate features | B3 | Dedup on `eventId` and on `transactionId`; ledger invariants for refunds/reversals | `test_ac001_duplicate_event_and_transaction_are_noops`, `test_ac001_full_replay_is_idempotent` | Mitigated |
| T-6 | Features or cache altered directly in Redis | B4 | None locally (no AUTH). Redis AUTH + TLS + network policy; state is rebuildable from Postgres | `test_redis_state_rebuilds_exactly_from_postgres` | Open |
| T-7 | Training data poisoning through feedback | B5 | Training uses versioned datasets, gates compare against the live baseline, an Approver promotes, shadow/canary before full, guardrail rollback. No live-feedback training path exists yet | `test_gates_block_a_model_that_loses_to_baseline_or_blows_the_budget`, `test_guardrail_rolls_back_a_failing_live_model` | Partial |
| T-8 | Event contract changed silently, consumers misread data | B3 | Avro contracts, `BACKWARD_TRANSITIVE` check against every committed version in CI; every produced message round-tripped through its schema | `tests/test_contracts.py`, `scripts/check_avro_compat.py` | Mitigated |

## Repudiation

| ID | Threat | Control | Evidence | Status |
|---|---|---|---|---|
| R-1 | Operator denies changing a promo, promoting, erasing | Audit: actor, role, action, resource, outcome, changes, traceId; logins, logouts, denials and rejected promotions included; automatic actions use a `system:*` actor | `test_model_endpoints_are_audited`, `test_denials_are_audited`, `test_guardrail_rolls_back_a_failing_live_model` | Mitigated |
| R-2 | Dispute over what a customer was shown | Impressions stored with requestId, position and modelVersion; responses carry model, feature-schema and ranking-config versions (SERV-002) | `test_recommendation_endpoint_shape_and_cache`, `test_ac007_preview_leaves_no_impression_and_no_cache` | Mitigated |

## Information disclosure

| ID | Threat | Boundary | Control | Evidence | Status |
|---|---|---|---|---|---|
| I-1 | A customer reads another customer's recommendations (BOLA) | B1 | Token subject must equal the path customer | `test_ac006_customer_token_cannot_read_another_customer` | Mitigated |
| I-2 | Staff read more than their role needs | B2 | Least-privilege permission map; every admin route guarded; profile reads audited | `test_rbac_matrix_every_role_every_route`, `test_every_admin_route_is_guarded` | Mitigated |
| I-3 | Credentials or card numbers in logs | all | No PAN in any contract; JSON logs pass a redaction filter (bearer, cookies, DSN passwords, 13–19 digit runs) | `test_logs_redact_credentials` | Mitigated |
| I-4 | Internals leaked in errors | B1, B2 | Uniform error body (code, message, traceId); fallback diagnostics redacted and admin-only | `test_errors_are_uniform_and_requests_are_bounded` | Mitigated |
| I-5 | Unauthenticated internal endpoints reachable: ranking `:8100`, MLflow `:5000`, Prometheus `:9090`, `/metrics`, Keycloak admin with default password | B4, B5 | Published on host ports for local work only. Production: cluster-internal services, network policies, SSO in front of MLflow/Prometheus, no default credentials | — | Open |
| I-6 | Traffic read in transit | all | Plaintext locally. TLS on every external and internal hop | — | Open |
| I-7 | Erased customer's data survives | B3, B4 | AC-009 maker-checker erasure with tombstones honoured by stream, reload, rebuild and training. Kafka retention, on-disk datasets, old MLflow artifacts and backups taken before the erasure are outside it | `test_ac009_erased_customer_is_not_rematerialised_by_replay` | Partial |
| I-8 | Bulk export of customer data | B2 | No export endpoint; list endpoints capped at 200 rows and paginated | — | Mitigated |

## Denial of service

| ID | Threat | Boundary | Control | Evidence | Status |
|---|---|---|---|---|---|
| D-1 | Flooding the customer API | B1 | Cache in front of recomputation; no rate limiting in the app (`429` is defined but unused) — belongs at the API gateway | — | Open |
| D-2 | Oversized requests | B1, B5 | `limit ≤ 20`, ≤ 200 candidates, score batch ≤ 500, list pages ≤ 200 | `test_errors_are_uniform_and_requests_are_bounded` | Mitigated |
| D-3 | Slow or failing ranking takes serving down | B5 | 400 ms client timeout, fallback to baseline, guardrail auto-rollback | `test_ac004_fallback_when_ranking_dependency_fails`, `test_guardrail_rolls_back_a_failing_live_model`, live drill | Mitigated |
| D-4 | Poison event stalls the consumer | B3 | Per-event isolation, quarantine + DLQ, consumer keeps going | `test_pipeline_applies_and_quarantines_per_spec`, `scripts/chaos.sh` | Mitigated |
| D-5 | Ranking service memory exhausted by warming many versions | B5 | Unauthenticated `warm` endpoint, unbounded booster cache. Network policy so only the API can call it; bound the cache | — | Open |
| D-6 | Simulator used to flood production | B3 | Disabled outside local/staging (SIM-003), capped TPS, Platform Operator only | `test_simulator_*` | Mitigated |

## Elevation of privilege

| ID | Threat | Control | Evidence | Status |
|---|---|---|---|---|
| E-1 | One person trains and promotes a model | Training and promotion are different roles; an identity with two application roles is refused at login | `test_promotion_requires_approver_and_passing_gates`, `test_one_identity_one_role` | Mitigated |
| E-2 | Operator approves their own erasure | Requester ≠ approver, enforced on the backend | `test_ac009_erased_customer_is_not_rematerialised_by_replay` | Mitigated |
| E-3 | A new admin route ships without a guard | RBAC matrix test fails CI for any unguarded `/admin` route | `test_every_admin_route_is_guarded` | Mitigated |
| E-4 | Compromised app process gains DBA powers | App currently connects as the table owner. Provision a non-owner role with only DML grants | — | Open |
| E-5 | Vulnerable dependency or tampered image | bandit, pip-audit, `npm audit`, SBOM in CI. Image signing and registry scanning are not in place | CI | Partial |

## Top open risks, in order

1. **S-2** — the customer API has no production authentication. Nothing customer-facing
   can go live until channel tokens are verified.
2. **S-3 / I-6 / T-6 / I-5** — no transport security or service authentication on
   Kafka, Redis and internal HTTP services. Mostly platform configuration.
3. **T-4** — model artifacts are not integrity-checked between approval and load.
4. **D-1 / D-5** — no rate limiting at the edge, and an unauthenticated internal endpoint
   that can exhaust ranking memory.
5. **E-4 / T-1** — the app's database role can undo the audit protection.
