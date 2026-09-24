from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
    model_version: str = "baseline-1.0.0"

    # Demo auth. Replace with the BFF session + OIDC upstream in Fase 5.
    admin_tokens: str = "admin-token:admin:Platform Operator,analyst-token:analyst:Analyst"
    customer_token_prefix: str = "cust-"


settings = Settings()
