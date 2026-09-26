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
    redis_max_connections: int = 64
    pg_max_connections: int = 20
    mlflow_tracking_uri: str = "sqlite:///./data/mlflow.db"
    mlflow_experiment: str = "merchant-ranking"
    model_dir: str = "./data/models"
    ranking_service_url: str = "http://localhost:8100"
    ranking_timeout_ms: int = 400
    # Batches are small (<=200 rows); a wide thread pool costs more to start than it saves.
    ranking_threads: int = 2

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

    # Canary guardrail (SDD 17.2 step 14): automatic rollback when the live model misbehaves.
    guardrail_interval_seconds: int = 30
    guardrail_min_requests: int = 50
    guardrail_max_degraded_rate: float = 0.05
    guardrail_max_p95_ms: float = 500.0

    # Continuous learning, stage 1: retrain on live feedback. 0 disables the loop.
    auto_retrain_interval_hours: float = 0.0
    auto_retrain_min_new_impressions: int = 500
    # Continuous learning, stage 2: online bandit beside every request, never served.
    online_bandit_enabled: bool = False
    online_bandit_exploration: float = 1.0
    online_bandit_learn_interval_seconds: int = 300
    # Promo uplift experiment (ADR-0010): share of customers served without promo offers.
    # 0 = off. Keep it fixed while an experiment runs; 100 would leave no treatment arm.
    promo_holdout_percent: int = Field(0, ge=0, le=99)


DEV_ENVIRONMENTS = {"local", "test", "ci"}


settings = Settings()
