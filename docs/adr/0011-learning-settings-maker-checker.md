# ADR-0011 — Maker-checker for the learning settings

**Status:** Accepted · 2026-09-26

## Context
The learning switches (auto-retrain interval, threshold and export retention; the online
bandit and its exploration; the promo-holdout split) decide which models get trained and
which customers go without offers. They were env vars: whoever deploys could change them.
Changes were audited and alerted when a replica started (threat T-10), but nobody approved
them first, and turning a loop on needed a restart.

## Decision
1. **Same pattern as erasure.** An ML Engineer or Platform Operator files a change with a
   reason (`learning:request`); an Approver decides (`learning:approve`); the requester can
   never decide their own. One request may be pending at a time. Only the six known
   settings are accepted, each bounded (`LearningChanges`).
2. **The database wins over env after the first approval.** Approval stores the *full* set
   of values, not just the change, so from then on no env value decides any switch. Env
   only gives a new deployment its starting values.
3. **Applied without a restart.** Each replica re-reads the stored row every 15 s and
   applies it to the shared `settings` object, so every reader is unchanged. The learning
   loops always run and check their switch each turn.
4. **Audited.** A request, an approval (as `config.learning` with before, after, requester
   and reason) and a rejection each write an audit row. Because approvals use the same
   action as the startup drift check, a restart after an approved change reports nothing.

## Consequences
- Until the first approved change, env still decides and is only audited and alerted.
  Going live should include one approved change, even if it restates the env values.
- An Approver stands in for the business and legal sign-off a promo holdout needs; the
  release gate still asks for that sign-off before the split goes above 0.
- Pending requests do not expire; a stale one must be rejected by hand.
- A replica can serve up to 15 s on the previous values after an approval.
