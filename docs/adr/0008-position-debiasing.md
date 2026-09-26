# ADR-0008 — Position debiasing with unbiased LambdaMART

**Status:** Accepted · 2026-09-26

## Context
Labels come from clicks, and a customer clicks the top of a slate more whether or not it
is the better merchant. Training on raw clicks teaches the model to copy whatever ranker
produced the logs. Every impression records its display position, and the generator
applies a known position bias of `1/log2(position+2)` (SYN-004), so a debiasing method can
be checked against the truth here, not just believed.

## Decision
1. Train with XGBoost's built-in unbiased LambdaMART (`lambdarank_unbiased=true`): it
   estimates click propensity per position jointly with the ranker and discounts pairs
   by it. No new dependency; serving is unchanged because position is still not a feature
   (ADR-0005).
2. XGBoost reads row order inside a query group as the display position, so `_dmatrix`
   sorts rows by `(requestId, position)`. Before this, rows followed event order and any
   `lambdarank_unbiased` passed through the training API was silently wrong.
3. `lambdarank_bias_norm=0.5`. On the generator's data it recovered the known curve within
   MAE 0.03 (0.038 on the small test dataset); XGBoost's default 1.0 flattened it to
   MAE 0.11. Offline NDCG was within noise of the biased model on the same split.
4. Each model records its estimated curve as `artifacts.positionBias` and in MLflow as
   `position_bias.json`, so a reviewer sees what the model believes about position.

## Consequences
- The norm was chosen where the truth is known, i.e. on synthetic data. On live logs it
  must be re-checked; a randomised swap of adjacent positions would give ground truth.
- Offline NDCG is computed on biased clicks, so it understates a debiased model; the
  gates stay as they are rather than being loosened on that argument.
- A test fails if rows are not in shown order or if the estimate drifts from the curve.
