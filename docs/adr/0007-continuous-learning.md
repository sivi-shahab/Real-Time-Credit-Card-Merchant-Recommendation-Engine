# ADR-0007 — Continuous learning: automatic retrain, human promotion

**Status:** Accepted (stage 1) · 2026-09-26

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
3. **Stage 2 — online bandit in SHADOW (planned).** A `river` Bayesian linear model scores
   candidates with an upper confidence bound and updates per labelled impression, compared
   against the served order like today's shadow model. It is not allowed to serve.

## Consequences
- `previous_version` now only records a model that was serving (CANARY/FULL). Before this,
  shadowing twice and rolling back put the earlier shadow model on FULL traffic without an
  Approver; with automatic shadowing that path would have been routine.
- Each run writes a full snapshot dir; pruning old `live-*` dirs is left for when disk
  matters. Export reads whole tables and will need to become incremental at volume.
- Automatic jobs and promotions are audited as `system:auto-retrain` with outcome
  `AUTOMATIC`.
