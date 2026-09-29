# 02 — Skema dan role analitik

Status: resolved
Type: task
Owner: tim aplikasi

## Yang dikerjakan
- Skema `analytics` berisi view untuk pertanyaan 1–6 (lihat spec), tanpa `envelope` dan teks bebas.
- Role `rec_analytics` hanya `SELECT` di skema itu, dibuat oleh job migrasi.
- Materialized view harian untuk agregat berat, di-refresh oleh worker.

## Kriteria selesai
Test seperti `test_the_app_role_has_data_access_only`: `rec_analytics` bisa membaca view, tidak
bisa membaca `transaction_log.envelope` atau tabel operasional mana pun.

## Comments

2026-09-29 — Skema `analytics` (17 view, tanpa `envelope`, `reject_detail`, `changes`,
`artifact_path`, nama staf atau catatan bebas) dan role `rec_analytics` (hanya `SELECT` di
skema itu, dibuat job migrasi) ada di `db/schema.sql`;
`test_the_analytics_role_reads_the_views_and_nothing_else` lolos. Materialized view harian
belum dibuat: belum ada query yang berat (terlama: `impression_reasons` ±3,7 detik di data
lokal); buat bila dashboard terasa lambat, di-refresh oleh worker.
