-- Source of truth for master data, jobs, and audit (SDD 3.3).
CREATE TABLE IF NOT EXISTS merchants (
  merchant_id   TEXT PRIMARY KEY,
  merchant_name TEXT NOT NULL,
  category_code TEXT NOT NULL,
  city_code     TEXT NOT NULL,
  channel       TEXT NOT NULL CHECK (channel IN ('ONLINE','OFFLINE')),
  rating        NUMERIC(3,2) NOT NULL CHECK (rating BETWEEN 0 AND 5),
  status        TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','INACTIVE')),
  version       INT  NOT NULL DEFAULT 1,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS merchants_city_cat ON merchants (city_code, category_code);

CREATE TABLE IF NOT EXISTS promotions (
  promotion_id        TEXT PRIMARY KEY,
  merchant_id         TEXT NOT NULL REFERENCES merchants(merchant_id),
  benefit_type        TEXT NOT NULL,
  benefit_value       NUMERIC(6,2) NOT NULL,
  min_spend_minor     BIGINT NOT NULL DEFAULT 0 CHECK (min_spend_minor >= 0),
  max_benefit_minor   BIGINT,
  eligible_card_tiers TEXT[] NOT NULL DEFAULT '{}',
  eligible_city_codes TEXT[] NOT NULL DEFAULT '{}',
  starts_at           TIMESTAMPTZ NOT NULL,
  ends_at             TIMESTAMPTZ NOT NULL,
  campaign_quota      INT NOT NULL DEFAULT 0 CHECK (campaign_quota >= 0),
  quota_used          INT NOT NULL DEFAULT 0 CHECK (quota_used >= 0),
  per_customer_limit  INT NOT NULL DEFAULT 1,
  status              TEXT NOT NULL DEFAULT 'ACTIVE',
  version             INT NOT NULL DEFAULT 1,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS promotions_merchant ON promotions (merchant_id, status);

CREATE TABLE IF NOT EXISTS customers (
  customer_id             TEXT PRIMARY KEY,
  city_code               TEXT NOT NULL,
  card_tier               TEXT NOT NULL,
  segment                 TEXT,
  personalization_allowed BOOLEAN NOT NULL DEFAULT TRUE
);

-- Transaction Explorer (UI-004): processed + quarantined events.
CREATE TABLE IF NOT EXISTS transaction_log (
  event_id       TEXT PRIMARY KEY,
  transaction_id TEXT NOT NULL,
  customer_id    TEXT NOT NULL,
  merchant_id    TEXT NOT NULL,
  txn_type       TEXT NOT NULL,
  amount_minor   BIGINT NOT NULL,
  occurred_at    TIMESTAMPTZ NOT NULL,
  received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  outcome        TEXT NOT NULL,          -- APPLIED | DUPLICATE_* | QUARANTINED
  reject_code    TEXT,
  reject_detail  TEXT,
  correlation_id TEXT,
  envelope       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS txnlog_customer_time ON transaction_log (customer_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS txnlog_outcome ON transaction_log (outcome, received_at DESC);
CREATE INDEX IF NOT EXISTS txnlog_txn ON transaction_log (transaction_id);

CREATE TABLE IF NOT EXISTS dataset_jobs (
  dataset_id  TEXT PRIMARY KEY,
  status      TEXT NOT NULL,            -- QUEUED | RUNNING | COMPLETED | FAILED
  config      JSONB NOT NULL,
  manifest    JSONB,
  error       TEXT,
  idempotency_key TEXT UNIQUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS simulation_runs (
  run_id      TEXT PRIMARY KEY,
  dataset_id  TEXT NOT NULL,
  status      TEXT NOT NULL,            -- CREATED|RUNNING|PAUSED|COMPLETED|STOPPED|FAILED
  target_tps  INT NOT NULL,
  offset_pos  BIGINT NOT NULL DEFAULT 0,
  total_events BIGINT NOT NULL DEFAULT 0,
  sent_count  BIGINT NOT NULL DEFAULT 0,
  failed_count BIGINT NOT NULL DEFAULT 0,
  actual_tps  NUMERIC(10,2) NOT NULL DEFAULT 0,
  speed_multiplier NUMERIC(6,2) NOT NULL DEFAULT 1,
  idempotency_key TEXT UNIQUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_events (
  id          BIGSERIAL PRIMARY KEY,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor       TEXT NOT NULL,
  actor_role  TEXT NOT NULL,
  action      TEXT NOT NULL,
  resource    TEXT NOT NULL,
  outcome     TEXT NOT NULL,
  changes     JSONB,
  trace_id    TEXT
);
CREATE INDEX IF NOT EXISTS audit_time ON audit_events (occurred_at DESC);
-- audit is append-only from the app's perspective (UI-009)
REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;

CREATE TABLE IF NOT EXISTS impressions (
  impression_id TEXT PRIMARY KEY,
  request_id    TEXT NOT NULL,
  customer_id   TEXT NOT NULL,
  merchant_id   TEXT NOT NULL,
  position      INT NOT NULL,
  model_version TEXT,
  occurred_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS impressions_request ON impressions (request_id);

CREATE TABLE IF NOT EXISTS interactions (
  interaction_id TEXT PRIMARY KEY,
  impression_id  TEXT NOT NULL,
  customer_id    TEXT NOT NULL,
  merchant_id    TEXT NOT NULL,
  interaction_type TEXT NOT NULL,       -- CLICK | PROMO_ACTIVATION | REDEMPTION
  occurred_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS interactions_impression ON interactions (impression_id);
