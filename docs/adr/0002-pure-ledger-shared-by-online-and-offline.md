# ADR-0002 — One pure ledger shared by the online and offline paths

**Status:** Accepted · 2026-09-24

## Context
AC-008 requires online aggregates to match an offline recomputation of the same events.
Two implementations of refund, reversal, deduplication and window expiry would drift,
and the reconciliation job would then be measuring the difference between two bugs.

## Decision
`rec/core/ledger.py` and `rec/core/features.py` hold all transaction and feature
arithmetic as pure functions over an in-memory `CustomerState`. The Kafka consumer and
`scripts/reconcile.py` both call them; only persistence differs.

## Consequences
- Reconciliation compares storage, not arithmetic — which is what can actually drift.
- Feature windows are evaluated as-of read time, so a dormant customer ages out of the
  90-day window with no scheduler and no new events (AC-003).
- `CustomerState` must fit in memory per customer. Buckets are keyed
  `(day, category, merchant)` and pruned at 90 days; the transaction ledger is kept 180
  days so late corrections still validate against their original.
