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

-- Serving keeps merchants and promotions in memory and re-reads them only when this
-- changes. A trigger, not application code, so a change made any way (API, master-data
-- load, redemption, SQL) reaches every API worker on its next request.
CREATE TABLE IF NOT EXISTS catalog_version (
  id      INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  version BIGINT NOT NULL DEFAULT 0
);
INSERT INTO catalog_version (id, version) VALUES (1, 0) ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION bump_catalog_version() RETURNS trigger AS $$
BEGIN
  UPDATE catalog_version SET version = version + 1 WHERE id = 1;
  RETURN NULL;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS merchants_catalog_version ON merchants;
CREATE TRIGGER merchants_catalog_version
  AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON merchants
  FOR EACH STATEMENT EXECUTE FUNCTION bump_catalog_version();

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
DROP TRIGGER IF EXISTS promotions_catalog_version ON promotions;
CREATE TRIGGER promotions_catalog_version
  AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON promotions
  FOR EACH STATEMENT EXECUTE FUNCTION bump_catalog_version();
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

-- ===== What customers were served, for analytics (Superset) =====
-- One row per response served (a cached response reuses its requestId, so it is not the
-- key). Written in batches off the request path; erasure deletes a customer's rows and
-- the worker prunes past SERVING_LOG_RETENTION_DAYS.
CREATE TABLE IF NOT EXISTS recommendation_log (
  log_id        UUID PRIMARY KEY,
  request_id    TEXT NOT NULL,
  customer_id   TEXT NOT NULL,
  served_at     TIMESTAMPTZ NOT NULL,
  model_version TEXT NOT NULL,
  source        TEXT NOT NULL CHECK (source IN ('LIVE','CACHE','FALLBACK')),
  personalized  BOOLEAN NOT NULL,
  cold_start    BOOLEAN NOT NULL,
  city_code     TEXT,
  channel       TEXT,
  item_count    INT NOT NULL
);
CREATE INDEX IF NOT EXISTS reclog_time ON recommendation_log (served_at);
CREATE INDEX IF NOT EXISTS reclog_customer ON recommendation_log (customer_id);
-- cached responses repeat their request id; impressions join back on it
CREATE INDEX IF NOT EXISTS reclog_request ON recommendation_log (request_id);
CREATE TABLE IF NOT EXISTS recommendation_log_items (
  log_id        UUID NOT NULL REFERENCES recommendation_log (log_id) ON DELETE CASCADE,
  position      INT NOT NULL,
  merchant_id   TEXT NOT NULL,
  category_code TEXT NOT NULL,
  reason_codes  TEXT[] NOT NULL,
  promotion_id  TEXT,
  PRIMARY KEY (log_id, position)
);

-- ===== Analytics schema for Superset (.scratch/superset-personalization-analytics) =====
-- Views only, read by the `rec_analytics` role, which has no access to the operational
-- tables. No raw envelopes and no free-text rejection details; customer ids are the same
-- pseudonymous ids the dashboard shows. Erased customers are gone from the base tables,
-- so they are gone here too.
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE OR REPLACE VIEW analytics.customers AS
  SELECT customer_id, city_code, card_tier, segment, personalization_allowed
  FROM customers;

CREATE OR REPLACE VIEW analytics.served_responses AS
  SELECT l.log_id, l.request_id, l.customer_id, l.served_at,
         l.served_at::date AS served_day, l.model_version, l.source, l.personalized,
         l.cold_start, l.city_code AS requested_city, l.channel, l.item_count,
         c.city_code AS customer_city, c.card_tier, c.segment
  FROM recommendation_log l LEFT JOIN customers c USING (customer_id);

CREATE OR REPLACE VIEW analytics.served_items AS
  SELECT i.log_id, l.served_at, l.customer_id, i.position, i.position + 1 AS rank,
         i.merchant_id, m.merchant_name, i.category_code, i.reason_codes,
         i.promotion_id, i.promotion_id IS NOT NULL AS has_promotion,
         l.model_version, l.source, l.personalized, c.segment, c.card_tier,
         c.city_code AS customer_city
  FROM recommendation_log_items i
  JOIN recommendation_log l USING (log_id)
  LEFT JOIN merchants m USING (merchant_id)
  LEFT JOIN customers c ON c.customer_id = l.customer_id;

CREATE OR REPLACE VIEW analytics.served_item_reasons AS
  SELECT i.log_id, l.served_at, i.position, i.category_code, r.reason_code,
         l.model_version, l.personalized
  FROM recommendation_log_items i
  JOIN recommendation_log l USING (log_id)
  CROSS JOIN LATERAL unnest(i.reason_codes) AS r(reason_code);

CREATE OR REPLACE VIEW analytics.impression_outcomes AS
  SELECT im.impression_id, im.request_id, im.customer_id, im.merchant_id,
         m.category_code, im.position, im.position + 1 AS rank, im.model_version,
         im.occurred_at AS shown_at,
         COALESCE(bool_or(ix.interaction_type = 'CLICK'), false) AS clicked,
         COALESCE(bool_or(ix.interaction_type = 'PROMO_ACTIVATION'), false) AS promo_activated,
         COALESCE(bool_or(ix.interaction_type = 'REDEMPTION'), false) AS redeemed,
         c.segment, c.card_tier, c.city_code AS customer_city
  FROM impressions im
  LEFT JOIN interactions ix USING (impression_id)
  LEFT JOIN merchants m ON m.merchant_id = im.merchant_id
  LEFT JOIN customers c ON c.customer_id = im.customer_id
  GROUP BY im.impression_id, m.category_code, c.segment, c.card_tier, c.city_code;

CREATE OR REPLACE VIEW analytics.transactions AS
  SELECT t.transaction_id, t.customer_id, t.merchant_id, m.merchant_name, m.category_code,
         m.city_code AS merchant_city, t.txn_type,
         -- IDR has no minor unit: amount_minor 150000 is IDR 150.000 (SDD 6.1)
         CASE WHEN t.txn_type = 'PURCHASE' THEN t.amount_minor ELSE -t.amount_minor END
           AS net_amount_idr,
         t.occurred_at, t.occurred_at::date AS occurred_day, t.received_at,
         EXTRACT(EPOCH FROM (t.received_at - t.occurred_at)) / 3600.0 AS lateness_hours,
         c.segment, c.card_tier, c.city_code AS customer_city,
         EXTRACT(HOUR FROM t.occurred_at AT TIME ZONE 'Asia/Jakarta')::int AS occurred_hour_wib
  FROM transaction_log t
  LEFT JOIN merchants m ON m.merchant_id = t.merchant_id
  LEFT JOIN customers c ON c.customer_id = t.customer_id
  WHERE t.outcome = 'APPLIED';

CREATE OR REPLACE VIEW analytics.ingestion_quality AS
  SELECT received_at, received_at::date AS received_day, outcome, reject_code, txn_type
  FROM transaction_log;

CREATE OR REPLACE VIEW analytics.shadow_evaluations AS
  SELECT request_id, model_version, served_source, rank_agreement, top1_agreement,
         model_latency_ms, occurred_at
  FROM shadow_evaluations;

CREATE OR REPLACE VIEW analytics.models AS
  SELECT model_version, created_at, approved, dataset_id, feature_schema_version,
         trainer_version,
         (metrics ->> 'ndcg@10')::float AS ndcg10,
         (baseline_metrics ->> 'ndcg@10')::float AS baseline_ndcg10,
         (metrics ->> 'ndcg@5')::float AS ndcg5,
         (baseline_metrics ->> 'ndcg@5')::float AS baseline_ndcg5,
         (metrics ->> 'inferenceLatencyMsP95')::float AS inference_p95_ms
  FROM models;

CREATE OR REPLACE VIEW analytics.model_deployment AS
  SELECT mode, model_version, previous_version, canary_percent, promoted_by, promoted_at, note
  FROM model_deployment;

CREATE OR REPLACE VIEW analytics.promotions AS
  SELECT p.promotion_id, p.merchant_id, m.merchant_name, m.category_code, p.benefit_type,
         p.benefit_value, p.starts_at, p.ends_at, p.campaign_quota, p.quota_used,
         CASE WHEN p.campaign_quota > 0
              THEN 100.0 * p.quota_used / p.campaign_quota END AS quota_used_pct,
         p.status
  FROM promotions p LEFT JOIN merchants m USING (merchant_id);

-- One row per step a promoted item reached, for a funnel chart: served with a promo ->
-- shown on screen (impression) -> activated -> redeemed.
CREATE OR REPLACE VIEW analytics.promo_funnel AS
  WITH offered AS (
    SELECT DISTINCT l.request_id, l.customer_id, i.merchant_id, i.promotion_id, l.served_at
    FROM recommendation_log_items i JOIN recommendation_log l USING (log_id)
    WHERE i.promotion_id IS NOT NULL
  ), shown AS (
    SELECT DISTINCT ON (im.impression_id) im.impression_id, o.promotion_id, im.occurred_at
    FROM impressions im
    JOIN offered o ON o.request_id = im.request_id AND o.merchant_id = im.merchant_id
  )
  SELECT '1. Ditawarkan' AS stage, promotion_id, served_at AS at FROM offered
  UNION ALL SELECT '2. Ditampilkan', promotion_id, occurred_at FROM shown
  UNION ALL SELECT '3. Diaktifkan', s.promotion_id, ix.occurred_at
    FROM shown s JOIN interactions ix USING (impression_id)
    WHERE ix.interaction_type = 'PROMO_ACTIVATION'
  UNION ALL SELECT '4. Ditukar', s.promotion_id, ix.occurred_at
    FROM shown s JOIN interactions ix USING (impression_id)
    WHERE ix.interaction_type = 'REDEMPTION';

CREATE OR REPLACE VIEW analytics.uplift_reports AS
  SELECT created_at, (report ->> 'customers')::int AS customers,
         (report ->> 'averageEffect')::float AS average_effect,
         (report -> 'qini' ->> 'model')::float AS qini_model,
         (report -> 'qini' ->> 'random')::float AS qini_random,
         (report -> 'segments' ->> 'persuadable')::float AS persuadable_share,
         (report -> 'segments' ->> 'sleepingDog')::float AS sleeping_dog_share
  FROM uplift_reports;

-- How customers responded per reason code. A cached response repeats its request id, and
-- every copy carries the same items, so one of them is enough.
CREATE OR REPLACE VIEW analytics.impression_reasons AS
  SELECT o.impression_id, o.shown_at, o.rank, o.model_version, o.category_code,
         r.reason_code, o.clicked, o.promo_activated, o.redeemed
  FROM analytics.impression_outcomes o
  CROSS JOIN LATERAL (
    SELECT i.reason_codes FROM recommendation_log l
    JOIN recommendation_log_items i USING (log_id)
    WHERE l.request_id = o.request_id AND i.merchant_id = o.merchant_id
    LIMIT 1) s
  CROSS JOIN LATERAL unnest(s.reason_codes) AS r(reason_code);

-- Recency and frequency per customer, bucketed; the number prefix orders the buckets.
-- value_decile: 1 = the tenth of transacting customers with the largest net spend.
CREATE OR REPLACE VIEW analytics.customer_activity AS
  SELECT c.customer_id, c.segment, c.card_tier, c.city_code AS customer_city,
         a.last_purchase_at, COALESCE(a.purchases_90d, 0) AS purchases_90d,
         CASE WHEN a.last_purchase_at IS NULL THEN '5. Belum pernah'
              WHEN a.last_purchase_at > now() - interval '7 days' THEN '1. 0-7 hari'
              WHEN a.last_purchase_at > now() - interval '30 days' THEN '2. 8-30 hari'
              WHEN a.last_purchase_at > now() - interval '90 days' THEN '3. 31-90 hari'
              ELSE '4. Lebih dari 90 hari' END AS recency_bucket,
         CASE WHEN COALESCE(a.purchases_90d, 0) = 0 THEN '1. 0'
              WHEN a.purchases_90d = 1 THEN '2. 1'
              WHEN a.purchases_90d <= 5 THEN '3. 2-5'
              WHEN a.purchases_90d <= 20 THEN '4. 6-20'
              ELSE '5. Lebih dari 20' END AS frequency_bucket,
         v.spend_idr, v.value_decile
  FROM customers c
  LEFT JOIN (
    SELECT customer_id, sum(net_amount_idr) AS spend_idr,
           ntile(10) OVER (ORDER BY sum(net_amount_idr) DESC) AS value_decile
    FROM analytics.transactions GROUP BY customer_id) v USING (customer_id)
  LEFT JOIN LATERAL (
    SELECT max(t.occurred_at) AS last_purchase_at,
           count(*) FILTER (WHERE t.occurred_at > now() - interval '90 days') AS purchases_90d
    FROM transaction_log t
    WHERE t.customer_id = c.customer_id AND t.outcome = 'APPLIED'
      AND t.txn_type = 'PURCHASE') a ON true;

-- Merchants ranked by net spend within each customer city.
CREATE OR REPLACE VIEW analytics.merchant_city_rank AS
  SELECT customer_city, merchant_id, merchant_name, category_code, spend_idr, customers,
         rank() OVER (PARTITION BY customer_city ORDER BY spend_idr DESC) AS rank_in_city
  FROM (SELECT customer_city, merchant_id, merchant_name, category_code,
               sum(net_amount_idr) AS spend_idr, count(DISTINCT customer_id) AS customers
        FROM analytics.transactions GROUP BY 1, 2, 3, 4) t;

-- Promotions and rollbacks from the audit trail: role, not the person, and no free text.
CREATE OR REPLACE VIEW analytics.model_deployment_history AS
  SELECT occurred_at, action, split_part(resource, '/', 2) AS model_version,
         COALESCE(changes ->> 'mode', changes ->> 'now') AS resulting_mode,
         (changes ->> 'canaryPercent')::int AS canary_percent, actor_role, outcome
  FROM audit_events
  WHERE action IN ('model.promote', 'model.rollback');

-- `rec_analytics`, when provisioned, reads the analytics views and nothing else.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rec_analytics') THEN
    GRANT USAGE ON SCHEMA analytics TO rec_analytics;
    GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO rec_analytics;
    REVOKE CREATE ON SCHEMA public FROM rec_analytics;
  END IF;
END $$;

-- ===== E-4: the application connects as `rec_app`, never as the owner =====
-- Provisioning creates the role (LOGIN, its own password); this grants it data access
-- only, re-applied on every migration so new tables are covered. Without ownership it
-- cannot drop the audit trigger or alter anything, and audit rows are insert-only.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rec_app') THEN
    GRANT USAGE ON SCHEMA public TO rec_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rec_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rec_app;
    REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM rec_app;
  END IF;
END $$;
