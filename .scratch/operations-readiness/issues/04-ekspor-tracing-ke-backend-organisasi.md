# 04 — Ekspor tracing ke backend organisasi

Status: blocked
Type: task
Owner: tim platform
Blocked by: 01

## Yang dikerjakan
Arahkan `OTEL_EXPORTER_OTLP_ENDPOINT` ke backend organisasi, sampling
`parentbased_traceidratio` 0,1 (runbook `tracing`).

## Kriteria selesai
Satu `traceId` dari audit row bisa dibuka di backend tracing organisasi.
