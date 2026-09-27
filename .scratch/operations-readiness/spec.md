# Spec — Kesiapan operasional

Status: waiting-external
Sumber: release gate "Alert routing (pager), on-call rota, dashboards in the ops tool"

## Tujuan
Setiap alert sampai ke orang yang tepat, dan kondisi sistem terlihat di tool ops organisasi.

## Kondisi sekarang
Prometheus memuat 14 aturan alert (unit test promtool di CI), masing-masing tertaut ke
runbook. Tracing lewat OpenTelemetry. Belum ada routing ke pager, rota on-call, atau
dashboard di tool ops.

## Requirement
1. R1 — Alertmanager (atau setara) merutekan alert `severity: page` ke pager dan
   `severity: ticket` ke antrean tiket.
2. R2 — Rota on-call dengan pemilik per layanan (api, ranking, stream, worker).
3. R3 — Dashboard di tool ops: latency dan error API, rasio fallback, lag consumer, kesehatan
   model dan guardrail, loop pembelajaran. Disimpan sebagai kode di repo bila tool-nya Grafana.
4. R4 — Tracing diekspor ke backend tracing organisasi dengan sampling 10%.

## Input yang ditunggu (organisasi)
Tool pager, tool dashboard, rota, backend tracing.
