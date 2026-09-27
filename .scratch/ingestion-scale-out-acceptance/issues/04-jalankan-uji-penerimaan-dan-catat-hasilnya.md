# 04 — Jalankan uji penerimaan dan catat hasilnya

Status: blocked
Type: task
Owner: tim platform + tim aplikasi
Blocked by: 02, 03

## Yang dikerjakan
- `scripts/loadtest.py kafka` terhadap topic dan database benchmark: 10.000/detik 30 menit,
  20.000/detik 5 menit.
- Catat throughput, lag maksimum, waktu pulih, CPU Redis per primary, CPU consumer, koneksi
  PgBouncer.

## Kriteria selesai
- Kedua skenario lulus, atau bottleneck baru teridentifikasi dengan data.
- Release gate baris ingestion diperbarui dengan angka dan kondisi uji; ADR-0014 status
  sizing dikonfirmasi atau direvisi.
