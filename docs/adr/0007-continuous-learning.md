# ADR-0007 — Continuous learning: automatic retrain, human promotion

**Status:** Accepted (stages 1 and 2) · 2026-09-26

## Context
Models were trained only on generator files, so live feedback (impressions and
interactions in Postgres) never reached a model. The goal is models that keep learning
from what customers actually do. ADR-0005 and SEC-001 still hold: only a gated model may
serve, and only an Approver may put one in front of customers.

## Decision
Continuous learning is staged.

1. **Stage 1 — automatic retrain (built).** `rec/api/auto_retrain.py` runs every
   `AUTO_RETRAIN_INTERVAL_HOURS` (0 = off, the default). When at least
   `AUTO_RETRAIN_MIN_NEW_IMPRESSIONS` impressions became observable since the last automatic
   job, it exports Postgres into a `live-*` dataset dir (`rec/ml/live_dataset.py`) and
   queues an ordinary training job with `trigger=auto`. Same builder, same gates, same
   registry: a test proves an export trains exactly like the generator files.
   Impressions inside their observation window are not exported, so none is labelled
   "ignored" before its click can arrive.
2. **The system may take a model to SHADOW, never further.** An approved automatic model is
   warmed and set to SHADOW only when the deployment is BASELINE or SHADOW. SHADOW serves
   the baseline, so no customer sees an unreviewed model. CANARY and FULL stay an
   Approver's decision; the system never replaces a serving model.
3. **Stage 2 — online bandit, shadow only (built).** `rec/ml/bandit.py`, enabled by
   `ONLINE_BANDIT_ENABLED`. One `river` `BayesianLinearRegression` over the serving
   feature vector scores candidates by mean + `ONLINE_BANDIT_EXPLORATION` × stdev (UCB), so
   learning about one merchant carries to similar ones. `LinUCBDisjoint` was rejected: one
   model per merchant starts cold for every merchant, and river documents it as too slow
   for practice. Beside every live request it stores the served items' vectors under the
   response requestId and records its own ordering in `shadow_evaluations` as
   `online-ucb`; every `ONLINE_BANDIT_LEARN_INTERVAL_SECONDS` one replica learns each
   impression whose observation window closed, exactly once (Redis watermark). It never
   serves: promoting it would be a new decision with its own ADR.

## Consequences
- `previous_version` now only records a model that was serving (CANARY/FULL). Before this,
  shadowing twice and rolling back put the earlier shadow model on FULL traffic without an
  Approver; with automatic shadowing that path would have been routine.
- Each run writes a full snapshot dir. Retention keeps the newest
  `AUTO_RETRAIN_KEEP_EXPORTS` plus any still in use or behind a model that serves or could
  be rolled back to, and audits each deletion (threat I-9). Export reads whole tables and
  will need to become incremental at volume.
- The bandit learns only from what the served ranker chose to show (logged, off-policy
  data), so its exploration term expresses what it would try, not what it has tried.
  Labels are taken when the observation window closes; conversions later in the
  attribution window reach the batch retrain but not the bandit.
- Bandit state is stored as JSON, not pickle, because Redis runs without AUTH here. That
  reads river's private attributes, so `river` is pinned exactly and a test round-trips it.
- The default shadow summary excludes `online-ucb`; pass `modelVersion` to see it.
- Automatic jobs and promotions are audited as `system:auto-retrain` with outcome
  `AUTOMATIC`.
