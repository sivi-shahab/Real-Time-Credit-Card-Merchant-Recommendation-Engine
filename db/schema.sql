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

-- ===== Fase 4: model lifecycle =====
CREATE TABLE IF NOT EXISTS training_jobs (
  job_id      TEXT PRIMARY KEY,
  dataset_id  TEXT NOT NULL,
  status      TEXT NOT NULL,            -- QUEUED | RUNNING | COMPLETED | FAILED
  params      JSONB NOT NULL DEFAULT '{}',
  result      JSONB,
  error       TEXT,
  idempotency_key TEXT UNIQUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS models (
  model_version   TEXT PRIMARY KEY,
  dataset_id      TEXT NOT NULL,
  job_id          TEXT REFERENCES training_jobs(job_id),
  feature_schema_version TEXT NOT NULL,
  trainer_version TEXT NOT NULL,
  artifact_path   TEXT NOT NULL,
  mlflow_run_id   TEXT,
  approved        BOOLEAN NOT NULL DEFAULT FALSE,
  metrics         JSONB NOT NULL DEFAULT '{}',
  baseline_metrics JSONB NOT NULL DEFAULT '{}',
  segment_metrics JSONB NOT NULL DEFAULT '{}',
  gates           JSONB NOT NULL DEFAULT '[]',
  lineage         JSONB NOT NULL DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one row: the live serving decision. ML-006 + SDD 17.2 compatibility matrix.
CREATE TABLE IF NOT EXISTS model_deployment (
  id              INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  mode            TEXT NOT NULL DEFAULT 'BASELINE'
                  CHECK (mode IN ('BASELINE','SHADOW','CANARY','FULL')),
  model_version   TEXT REFERENCES models(model_version),
  previous_version TEXT REFERENCES models(model_version),
  canary_percent  INT NOT NULL DEFAULT 0 CHECK (canary_percent BETWEEN 0 AND 100),
  promoted_by     TEXT,
  promoted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  note            TEXT
);
INSERT INTO model_deployment (id, mode) VALUES (1, 'BASELINE')
  ON CONFLICT (id) DO NOTHING;

-- Shadow comparisons: what the model would have ranked vs what was served.
CREATE TABLE IF NOT EXISTS shadow_evaluations (
  request_id     TEXT PRIMARY KEY,
  customer_id    TEXT NOT NULL,
  model_version  TEXT NOT NULL,
  served_source  TEXT NOT NULL,
  rank_agreement NUMERIC(5,4),
  top1_agreement BOOLEAN,
  model_latency_ms NUMERIC(8,2),
  occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS shadow_time ON shadow_evaluations (occurred_at DESC);

-- ===== Fase 5: hardening =====
-- Audit is append-only for the application role too (REVOKE above does not bind the
-- table owner). A DBA with owner rights can still drop this trigger; in production the
-- app connects as a non-owner role — see docs/runbooks.md.
CREATE OR REPLACE FUNCTION audit_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only';
END $$;
CREATE OR REPLACE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION audit_is_append_only();

-- AC-009 / SEC-002: erasure is maker-checker. The requester cannot approve their own.
CREATE TABLE IF NOT EXISTS erasure_requests (
  request_id   TEXT PRIMARY KEY,
  customer_id  TEXT NOT NULL,
  reason       TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'PENDING'
               CHECK (status IN ('PENDING','EXECUTED','REJECTED')),
  requested_by TEXT NOT NULL,
  decided_by   TEXT,
  result       JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at   TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS erasure_one_pending
  ON erasure_requests (customer_id) WHERE status = 'PENDING';

-- Tombstones: all that remains of an erased customer is the fact of erasure, so a
-- replay or master-data reload can refuse to materialise them again.
CREATE TABLE IF NOT EXISTS erased_customers (
  customer_id TEXT PRIMARY KEY,
  request_id  TEXT NOT NULL,
  erased_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Processing order for state rebuild. received_at is per flush batch since Fase 5
-- batching, so it cannot order a customer's events on its own. (expand-only change)
ALTER TABLE transaction_log ADD COLUMN IF NOT EXISTS seq BIGSERIAL;

-- ===== Promo holdout experiment (ADR-0010) =====
-- The first arm each customer was served in while the holdout ran. Customer data:
-- erasure deletes it. holdout_percent is kept so a changed split is visible in analysis.
CREATE TABLE IF NOT EXISTS promo_experiment (
  customer_id      TEXT PRIMARY KEY,
  arm              TEXT NOT NULL CHECK (arm IN ('TREATMENT','HOLDOUT')),
  holdout_percent  INT NOT NULL CHECK (holdout_percent BETWEEN 1 AND 99),
  first_exposed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Small training artifacts the dashboard reads: feature importance and the estimated
-- position-bias curve (ADR-0008). Before this they lived only in the metadata file.
ALTER TABLE models ADD COLUMN IF NOT EXISTS artifacts JSONB NOT NULL DEFAULT '{}';

-- SHA-256 of the model file, recorded at training and verified by the ranking service at
-- every load (threat T-4). NULL for models trained before it: those no longer serve.
ALTER TABLE models ADD COLUMN IF NOT EXISTS artifact_sha256 TEXT
  CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$');

-- Uplift reports from `python -m rec.ml.uplift` (ADR-0010): aggregates only, no
-- per-customer rows, so erasure has nothing to find here.
CREATE TABLE IF NOT EXISTS uplift_reports (
  id         BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  report     JSONB NOT NULL
);

-- ===== Learning settings maker-checker (ADR-0011, threat T-10) =====
-- Once a change is approved this row holds every learning setting and wins over env.
CREATE TABLE IF NOT EXISTS learning_settings (
  id              INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  settings_values JSONB NOT NULL,
  version         INT NOT NULL,
  updated_by      TEXT NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS learning_setting_requests (
  request_id   TEXT PRIMARY KEY,
  changes      JSONB NOT NULL,
  reason       TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status       TEXT NOT NULL DEFAULT 'PENDING',
  decided_by   TEXT,
  decided_at   TIMESTAMPTZ,
  note         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS learning_one_pending
  ON learning_setting_requests ((true)) WHERE status = 'PENDING';
-- Pending requests expire (ADR-0011). Re-created each start so databases made before
-- EXPIRED existed get it too.
ALTER TABLE learning_setting_requests
  DROP CONSTRAINT IF EXISTS learning_setting_requests_status_check;
ALTER TABLE learning_setting_requests ADD CONSTRAINT learning_setting_requests_status_check
  CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED'));
