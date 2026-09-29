# 06 — Tinjauan privasi dashboard analitik

Status: waiting-external
Type: task
Owner: tim aplikasi + legal
Blocked by: 04

## Yang dikerjakan
Periksa kolom yang terekspos, pembatasan export CSV, retensi log penyajian, dan audit akses
Superset terhadap UU PDP; perbarui threat model.

## Kriteria selesai
Threat model memuat baris untuk Superset; keputusan legal tercatat di `## Comments`.

2026-09-29 — Tiket 04 selesai. Bagian tim aplikasi sudah ada: threat model memuat B7, I-8
(export CSV hanya Platform Operator) dan I-10 (dashboard analitik); retensi log penyajian 180
hari (`SERVING_LOG_RETENTION_DAYS`); akses dashboard tercatat di tabel `logs` Superset.
Menunggu legal: tinjauan kolom yang terekspos terhadap UU PDP dan keputusan yang dicatat di sini.
