# 02 — Skema dan role analitik

Status: ready
Type: task
Owner: tim aplikasi

## Yang dikerjakan
- Skema `analytics` berisi view untuk pertanyaan 1–6 (lihat spec), tanpa `envelope` dan teks bebas.
- Role `rec_analytics` hanya `SELECT` di skema itu, dibuat oleh job migrasi.
- Materialized view harian untuk agregat berat, di-refresh oleh worker.

## Kriteria selesai
Test seperti `test_the_app_role_has_data_access_only`: `rec_analytics` bisa membaca view, tidak
bisa membaca `transaction_log.envelope` atau tabel operasional mana pun.
