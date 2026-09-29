# 04 — Ekspor tracing ke backend organisasi

Status: blocked
Type: task
Owner: tim platform
Blocked by: 01

## Yang dikerjakan
Arahkan `OTEL_EXPORTER_OTLP_ENDPOINT` ke backend organisasi, sampling
`parentbased_traceidratio` 0,05 (runbook `tracing`; 0,1 diturunkan 2026-09-29 setelah
pengukuran p95, lihat `superset-personalization-analytics` tiket 01).

## Kriteria selesai
Satu `traceId` dari audit row bisa dibuka di backend tracing organisasi.
