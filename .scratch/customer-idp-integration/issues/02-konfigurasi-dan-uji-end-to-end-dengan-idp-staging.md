# 02 — Konfigurasi dan uji end-to-end dengan IdP staging

Status: blocked
Type: task
Owner: tim aplikasi
Blocked by: 01

## Yang dikerjakan
- Isi `CUSTOMER_JWKS_URL`, `CUSTOMER_JWT_ISSUER`, `CUSTOMER_JWT_AUDIENCE` (dan
  `CUSTOMER_ID_CLAIM` bila bukan `sub`) untuk staging.
- Tambah skrip uji `scripts/smoke_customer_auth.sh` (atau test yang di-skip tanpa env IdP)
  yang mengambil token uji dan menjalankan: GET rekomendasi, POST impressions, POST klik.
- Uji penolakan: token kedaluwarsa, audience salah, issuer salah, token customer lain (403).

## Kriteria selesai
- Semua skenario di atas lulus terhadap IdP staging, hasilnya dicatat di `## Comments`.
- Release gate baris S-2 diubah ke PASS untuk staging, dengan rujukan skripnya.
