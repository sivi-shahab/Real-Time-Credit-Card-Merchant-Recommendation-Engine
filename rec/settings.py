from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # SEC-002: secrets may come from files mounted by a secret manager / docker secrets
    # (/run/secrets/<field_name>) instead of plain env vars.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore",
        secrets_dir="/run/secrets" if Path("/run/secrets").is_dir() else None)

    kafka_bootstrap: str = "localhost:9092"
    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = "postgresql://rec:rec@localhost:5432/rec"

    data_dir: str = "./data"
    environment: str = "local"  # SIM-003: simulator disabled outside local/staging

    topic_transactions: str = "cc.transactions"
    topic_normalized: str = "cc.transactions.normalized"
    topic_features: str = "customer.features"
    topic_dlq: str = "pipeline.dlq"
    topic_impressions: str = "recommendation.impressions"
    topic_interactions: str = "recommendation.interactions"

    recommendation_cache_ttl_seconds: int = 300  # SERV-004
    dedup_ttl_seconds: int = 60 * 60 * 24 * 7
    max_simulator_tps: int = 5000  # SIM-003
    stream_concurrency: int = 32
    # Consumers scale by partition within one group (ADR-0014); several on one host need
    # their own metrics ports.
    stream_group_id: str = "feature-engine"
    stream_metrics_port: int = 9102
    redis_max_connections: int = 64
    # ADR-0014: REDIS_URL names any cluster node; keys are hash-tagged per customer.
    redis_cluster: bool = False
    pg_max_connections: int = 20
    # Apply db/schema.sql at startup (dev: the app role owns the schema). Production sets
    # false and runs `python -m rec.store.pg` as the owner before deploying (E-4).
    db_auto_migrate: bool = True
    # ADR-0014: 0 behind PgBouncer in transaction pooling without prepared-statement
    # support (rec.store.pg._pooler_options). The migration job connects to Postgres
    # directly (it holds a session advisory lock).
    pg_statement_cache_size: int = 100
    # Local only: lets the migration job create the `rec_app` login role with this
    # password. Production provisions that role itself and leaves this empty.
    app_db_password: str = ""
    mlflow_tracking_uri: str = "sqlite:///./data/mlflow.db"
    mlflow_experiment: str = "merchant-ranking"
    model_dir: str = "./data/models"
    ranking_service_url: str = "http://localhost:8100"
    ranking_timeout_ms: int = 400
    # Batches are small (<=200 rows); a wide thread pool costs more to start than it saves.
    ranking_threads: int = 2
    # D-5: boosters kept in memory by the ranking service (live, previous, shadow/canary
    # fit easily); an evicted one is reloaded, and re-verified, on its next use.
    ranking_max_models: int = 4
    # D-5: shared secret the API sends to the ranking service's /v1 endpoints. Empty is
    # accepted only in dev environments.
    ranking_service_token: str = ""

    # Static bearer tokens: scripts and tests only. Ignored outside DEV_ENVIRONMENTS, where
    # admins sign in through the BFF (OIDC) and machines need a real credential.
    admin_tokens: str = (
        "admin-token:admin:Platform Operator,analyst-token:analyst:Analyst,"
        "ops-token:ops:Marketing Operator,auditor-token:auditor:Auditor,"
        "ml-token:mlops:ML Engineer,approver-token:approver:Approver,"
        "viewer-token:viewer:Viewer"
    )
    customer_token_prefix: str = "cust-"

    # SDD 13.4 BFF. Browser auth is an opaque server-side session in an HttpOnly cookie.
    session_cookie: str = "rec_session"
    session_idle_seconds: int = 30 * 60
    session_absolute_seconds: int = 8 * 60 * 60
    cookie_secure: bool = True  # only .env for plain-http local turns this off
    # OIDC (authorization code + PKCE). Empty issuer = SSO disabled.
    oidc_discovery_url: str = ""  # back-channel URL of .well-known/openid-configuration
    oidc_client_id: str = "rec-dashboard"
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:5173/bff/callback"
    oidc_roles_claim: str = "roles"
    # Customer channel (S-2, ADR-0013): the mobile app's OIDC access token, a JWT verified
    # against the customer IdP's keys. All three empty = customer tokens refused.
    customer_jwks_url: str = ""
    customer_jwt_issuer: str = ""
    customer_jwt_audience: str = ""
    customer_id_claim: str = "sub"  # the claim that holds our customerId
    # D-1: requests per customer per minute, across all API workers. 0 = unlimited.
    rate_limit_recommendations_per_minute: int = 60
    rate_limit_feedback_per_minute: int = 120

    # Canary guardrail (SDD 17.2 step 14): automatic rollback when the live model misbehaves.
    guardrail_interval_seconds: int = 30
    guardrail_min_requests: int = 50
    guardrail_max_degraded_rate: float = 0.05
    guardrail_max_p95_ms: float = 500.0

    # Continuous learning, stage 1: retrain on live feedback. 0 disables the loop.
    auto_retrain_interval_hours: float = 0.0
    auto_retrain_min_new_impressions: int = 500
    # I-9: live exports kept on disk after each run, besides those still in use.
    auto_retrain_keep_exports: int = Field(3, ge=1)
    # Continuous learning, stage 2: online bandit beside every request, never served.
    online_bandit_enabled: bool = False
    online_bandit_exploration: float = 1.0
    online_bandit_learn_interval_seconds: int = 300
    # Worker process (ADR-0012): training queue, scheduled uplift report, learning loops.
    training_poll_seconds: float = 5.0
    training_timeout_hours: float = 6.0
    uplift_report_interval_hours: float = 24.0  # 0 disables the scheduled report
    worker_metrics_port: int = 9103
    # Promo uplift experiment (ADR-0010): share of customers served without promo offers.
    # 0 = off. Keep it fixed while an experiment runs; 100 would leave no treatment arm.
    promo_holdout_percent: int = Field(0, ge=0, le=99)


DEV_ENVIRONMENTS = {"local", "test", "ci"}


settings = Settings()
