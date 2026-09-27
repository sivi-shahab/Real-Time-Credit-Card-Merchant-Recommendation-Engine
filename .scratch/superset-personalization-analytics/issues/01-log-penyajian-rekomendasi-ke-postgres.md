# 01 — Log penyajian rekomendasi ke Postgres

Status: ready
Type: task
Owner: tim aplikasi

## Yang dikerjakan
- Tabel `recommendation_log` dan `recommendation_log_items` (lihat R1 di spec).
- Penulisan di luar jalur latensi: antrean in-process yang di-flush per batch, atau lewat
  Kafka; preview admin tidak dicatat (AC-007).
- Erasure menghapus baris nasabah; retensi via setting (misalnya 180 hari).

## Kriteria selesai
- Test: respons nasabah tercatat lengkap dengan kode alasan dan promo; preview tidak tercatat;
  erasure menghapus barisnya.
- p95 API pada 32 klien tetap < 200 ms (ukur ulang seperti di release gate).
