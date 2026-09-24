# ADR-0004 — Per-dataset ID namespace for independent experiments

**Status:** Accepted · 2026-09-24

## Context
SIM-002 requires an independent experiment to use a new ID namespace. Without one, two
datasets generated from the same seed emit identical `eventId` and `transactionId`
values, and the second replay is silently swallowed whole by deduplication — the
pipeline looks healthy while consuming nothing. This was observed in practice before the
namespace existed.

## Decision
`DatasetConfig.idNamespace` prefixes customer, merchant and promotion IDs and is salted
into the deterministic UUIDs behind `eventId` and `transactionId`. It defaults to empty,
so SYN-006 reproducibility (same config + seed + version ⇒ identical checksums) is
unchanged.

## Consequences
- Reconciliation against a specific dataset is exact, because its customers are disjoint
  from every other dataset's.
- The merchant catalog remains shared master data in Postgres; namespacing scopes an
  experiment, it does not partition the catalog.
- Operators running a second experiment must set `idNamespace`, or accept that the run
  accumulates onto the previous state.
