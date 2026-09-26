# ADR-0009 — Hyperparameter tuning, and AutoML as offline challengers only

**Status:** Accepted · 2026-09-26

## Context
AutoML stacks were proposed for the ranker: LightAutoML, CatBoost + Optuna, and CausalML
for uplift (ADR-0010). The production path is XGBoost unbiased LambdaMART (ADR-0008) over
29 numeric features built by one vectoriser (ADR-0005), loaded by a ranking service that
only reads XGBoost boosters. Raw `merchantId`/MCC are deliberately not features, so a new
merchant is scored from its category, rating and promo instead of an unseen id.

## Decision
1. **Optuna tunes the production trainer, opt-in.** `tuneTrials` on a training job runs a
   seeded TPE search (eta, depth, min child weight, subsampling, L2) maximising NDCG@5 on
   a validation slice of the training period. Parameters the caller sets are held fixed.
   The result goes through the same gates and Approver; the search is in the lineage.
2. **Early stopping moved off the test side.** It used the test split the gates grade, so
   the gates judged a model partly chosen on their own data. Training now carves a later
   20% of the training period as validation for early stopping and tuning.
3. **CatBoost and LightAutoML are offline challengers** (`python -m rec.ml.benchmark`),
   graded on `train()`'s exact splits and metrics. Neither is on the serving path.
   - CatBoost is an optional extra (`.[benchmark]`).
   - LightAutoML lives in its own env (`.venv-automl`, CPU torch): installing it
     downgrades xgboost 3.4.1 to 2.1.4, pulls torch + 16 CUDA packages, does not
     support pandas 3, has no learning-to-rank task, and persists pipelines as pickles.

## Measured (synthetic, one seed, 300 customers / 6k transactions)
| ranker | NDCG@5 | NDCG@10 |
|---|---|---|
| baseline | 0.3987 | 0.4911 |
| xgboost (production, debiased) | 0.4433 | 0.5210 |
| xgboost + Optuna 20 trials | 0.4454 | 0.5236 |
| xgboost, not debiased | 0.4394 | 0.5193 |
| catboost YetiRank | 0.4540 | 0.5301 |
| catboost + raw ids | 0.4560 | 0.5298 |
| lightautoml, pointwise, 300 s | 0.4638 | 0.5354 |
| lightautoml + raw ids | 0.4663 | 0.5353 |

## Consequences
- Raw categorical ids add ~0.002 for either challenger: the "native categorical" argument
  does not hold on this feature design. The gap is model family, not encoding.
- Test labels are raw clicks, which favour rankers that were not debiased; compare
  CatBoost with `xgboost-biased`, not with production.
- The generator draws clicks pointwise and additively, which suits pointwise learners.
  A challenger winning here is a reason to test it on live logs, not to switch. Switching
  family means a ranking service that loads it, feature parity, and a new ADR.
