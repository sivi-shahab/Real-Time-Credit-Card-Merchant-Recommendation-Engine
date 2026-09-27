# 03 — Alert untuk lonjakan penolakan token customer

Status: ready
Type: task
Owner: tim aplikasi

## Konteks
Kalau IdP merotasi kunci dan JWKS tidak terjangkau, atau konfigurasi audience salah, semua
nasabah mendapat 401 tanpa ada yang tahu.

## Yang dikerjakan
- Pastikan penolakan token customer terhitung terpisah dari 401 staf (misalnya label pada
  `http_requests_total` untuk route `/api/v1/*` dengan status 401, atau counter baru
  `customer_auth_rejected_total{reason}`).
- Alert Prometheus: rasio 401 di route customer > 5% selama 10 menit, dengan runbook.
- Unit test promtool di `deploy/prometheus/alerts_test.yml`.

## Kriteria selesai
Alert dan runbook ada, test promtool lulus di CI.
