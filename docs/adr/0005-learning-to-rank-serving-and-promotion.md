# ADR-0005 — Learning-to-Rank serving, promotion, and the baseline floor

**Status:** Accepted · 2026-09-25

## Context
Fase 4 adds an XGBoost `rank:ndcg` model. Three risks dominate: training/serving feature
skew, time leakage in the training set, and a model failure taking recommendations down
with it. The SDD also requires that a high offline score alone never promotes a model
(ML-006).

## Decision
1. **One vectoriser.** `rec/ml/vectorize.py` is the only place a feature row is built.
   The dataset builder and the serving path both call `vectorize` with the same keyword
   arguments; a test compares the two outputs for identical inputs. Position is
   deliberately not a feature — it is exposure bias and does not exist at ranking time.
2. **Structural point-in-time join.** Transactions and recommendation requests are merged
   into one event-time ordered timeline, and a request can only see ledger state built
   from events that preceded it. There is no filter to forget: a test appends a huge
   transaction dated after the last request and asserts every feature row is unchanged.
3. **Separate ranking service.** Inference runs in its own FastAPI process behind an HTTP
   timeout, so the serving path's degradation to baseline is exercised by a real failure
   rather than a simulated one.
4. **Baseline is the floor, not the alternative.** Any model failure — timeout, transport
   error, schema mismatch, length mismatch — degrades that request to the baseline
   ranking with `source=FALLBACK`. Reason codes always come from the baseline components,
   so the explanation shown to a customer does not change with the ranker.
5. **Promotion is gated and separated.** Only an `Approver` may promote, and only a model
   whose evaluation gates passed and whose feature schema matches serving. Promotion warms
   the artifact in the ranking service first and refuses if it cannot be loaded. The
   deployment row (`BASELINE`/`SHADOW`/`CANARY`/`FULL`) is the single serving decision and
   is part of the cache key.

## Consequences
- Canary assignment is `sha256(customerId) % 100`, so a customer never flips ranker
  between refreshes — at the cost of a fixed, non-reshufflable cohort.
- Shadow scoring is fire-and-forget: it cannot enter the customer latency budget, and a
  shadow failure is recorded as a counter rather than surfaced as a request error.
- `rank:ndcg` learns from the slate that was actually shown. Candidates never exposed are
  absent from training, not negative — which keeps the labels honest but means the model
  only learns to reorder what candidate generation already retrieved.
- Promo quota is evaluated from current `quota_used` when building historical rows; quota
  exhaustion cannot be reconstructed point-in-time from a static master table. Recorded
  as a known limitation in the dataset metadata.
