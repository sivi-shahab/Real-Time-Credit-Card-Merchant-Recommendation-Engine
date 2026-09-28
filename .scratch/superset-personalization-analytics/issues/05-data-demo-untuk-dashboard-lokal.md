# 05 — Data demo untuk dashboard lokal

Status: resolved
Type: task
Owner: tim aplikasi
Blocked by: 01

## Yang dikerjakan
Skrip yang memuat feedback dari dataset sintetis (`feedback_events.jsonl`) dan menjalankan
trafik rekomendasi serta feedback yang konsisten dengan slate yang disajikan (S-5).

## Kriteria selesai
Setiap dashboard punya data yang cukup untuk dibaca; skrip idempoten dan menolak berjalan di
luar local/test/ci.

## Comments

2026-09-29 — `scripts/demo_analytics.py`, namespace `demo` (terpisah dari sisa smoke/chaos):
dataset dibuat sekali, replay lewat Kafka (ulang = no-op, dedup T-5), feedback historis dari
`feedback_events.jsonl` dimuat dengan id dari file dan `model_version = synthetic`, lalu
trafik live lewat API (S-5), dilewati bila nasabah demo sudah pernah disajikan. Run pertama
±2 menit: 8.390 transaksi, 64.000 + 5.518 impression, 12.121 + 803 interaksi, 0 error.
Run kedua tidak menambah baris; `ENVIRONMENT=prod` ditolak. Ke-43 chart berisi data.
"Porsi event terlambat" 100% untuk data replay itu benar: definisinya sama dengan stream
(FEAT-003, >24 jam), dan riwayat 120 hari selalu tiba terlambat.
