# 02 — Verifikasi TLS dan AUTH untuk Redis dan Postgres

Status: ready
Type: task
Owner: tim aplikasi

## Yang dikerjakan
- Uji lokal dengan Redis `requirepass` + TLS (`rediss://`) dan Postgres `ssl=on`
  (`sslmode=verify-full`), termasuk mode Redis Cluster.
- Pastikan redaksi log menyembunyikan password di URL/DSN (`rec/obs.py`).
- Dokumentasikan format URL/DSN di runbook.

## Kriteria selesai
Suite lulus terhadap Redis dan Postgres ber-TLS/AUTH; tidak ada kredensial di log.
