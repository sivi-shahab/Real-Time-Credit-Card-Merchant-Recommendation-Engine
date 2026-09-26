# ADR-0010 — Promo holdout experiment and uplift estimation

**Status:** Accepted · 2026-09-26

## Context
Last-touch attribution credits a promo for every redemption, including customers who
would have bought anyway (ML-004; SDD §375: causal uplift needs a controlled experiment).
Every customer who asks for recommendations sees every eligible offer, so there is no
control group and no uplift can be estimated, whatever the library.

## Decision
1. **A randomised holdout, off by default.** `PROMO_HOLDOUT_PERCENT` (0-99) of customers,
   chosen by `sha256("promo-holdout:" + customerId)`, are served recommendations with no
   promotion offers. The salt keeps this split independent of the canary. A holdout
   response caches under its own key, so a cached treatment response cannot leak offers.
2. **Record the first arm.** While the holdout runs, each customer's first arm, the split
   and the time are written to `promo_experiment` (fire-and-forget, first row wins).
   Erasure deletes it with the rest of the customer's data.
3. **Estimate offline with CausalML** (`python -m rec.ml.uplift`, extra `.[causal]`).
   Unit: customer whose 14-day outcome window has closed. Treatment: offers shown.
   Outcome: a purchase at a promo merchant within the window, from the transaction stream,
   which records purchases whether or not anything was shown. Features: purchase history
   as of first exposure. An X-learner (XGBoost learners, propensity = the configured split,
   not an estimate) gives a per-customer effect; Qini on held-out customers is compared
   with a random ordering.
4. **Estimating is not acting.** Nothing withholds offers from predicted sure things or
   sleeping dogs. That is a targeting policy with its own review and ADR.

## Consequences
- Holdout customers lose offers for the whole experiment: size and duration are a
  business decision, and the split must stay fixed while it runs (the row keeps it).
- The generator's purchases do not respond to promotions, so on synthetic data the true
  uplift is zero; the estimator is tested on data with a planted heterogeneous effect.
- "No effect" joins sure things and lost causes; telling them apart needs the control
  outcome model as well, which the report does not claim to do.
- The online bandit's stored contexts are now keyed by customer so erasure removes them
  too (found while adding this table to erasure).
