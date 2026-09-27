# 04 — Go-live: konfigurasi produksi dan verifikasi

Status: blocked
Type: task
Owner: tim aplikasi + tim mobile
Blocked by: 02, 03

## Yang dikerjakan
- Isi konfigurasi produksi (nilai dari tiket 01) lewat secret manager.
- Jalankan skrip dari tiket 02 terhadap produksi dengan akun uji.
- Pastikan token `cust-<id>` tetap ditolak (`ENVIRONMENT` bukan local/test/ci).

## Kriteria selesai
Release gate baris S-2 berstatus PASS untuk produksi; threat model S-2 diperbarui.
