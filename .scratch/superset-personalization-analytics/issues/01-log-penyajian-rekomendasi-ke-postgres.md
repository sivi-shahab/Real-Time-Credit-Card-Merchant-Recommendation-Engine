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

## Comments

2026-09-29 — Ukur ulang p95, metode release gate: `ab` dari container di jaringan compose,
32 klien, API 8 worker, rate limit off, mode BASELINE, host 16 core. A/B tiga ronde:
log penyajian aktif vs `SERVING_LOG_MAX_BUFFER=0` (`record()` langsung keluar).

| Ronde | Hit, log | Hit, tanpa log | Miss, log | Miss, tanpa log |
|---|---|---|---|---|
| 1 | 191 ms | 171 ms | 468 ms | 481 ms |
| 2 | 182 ms | 256 ms | 633 ms | 606 ms |
| 3 | 227 ms | 225 ms | 566 ms | 589 ms |

Log penyajian tidak menambah latensi yang terukur: selisihnya lebih kecil dari variasi antar
ronde, dan tiap ronde dengan log mencatat semua 11 000 respons. Tapi angka absolut di atas
release gate (hit 101–168 ms, miss 145–173 ms), dengan atau tanpa log, dan juga untuk
nasabah dengan 11 transaksi (miss 532–628 ms). Penyebabnya belum diketahui; kandidat:
beban stack lokal (Superset, MLflow, Jaeger ikut jalan; RAM 11/15 GB terpakai) atau
regresi di commit lain sejak release gate. Status tetap `blocked` sampai itu jelas.

2026-09-29 — Lanjutan. Menghentikan Superset, MLflow, Kafka UI, dashboard dan Keycloak tidak
menurunkan p95 (hit 192–323 ms, miss 511–929 ms): bukan beban stack. Host sedang ramai
(`apt.systemd.daily`, load 7–11 tanpa trafik).

Pembanding: API di commit release gate (`b252b97`) dijalankan berdampingan (`api-gate`,
Redis db 2 dibangun ulang dari Postgres yang sama, rekomendasi identik), diukur bergantian.
Cache miss: sekarang 466–525 ms, release gate 581–610 ms, jadi angka miss yang tinggi
berasal dari host/data, bukan kode. Cache hit: sekarang 173–192 ms, release gate 78–96 ms,
regresi kode sejak release gate.

Toggle tracing (log penyajian aktif, cache hit, p95):

| Ronde | Tracing penuh | Tracing mati | Sampling 5% | Release gate |
|---|---|---|---|---|
| 1 | 185 ms | 149 ms | 152 ms | 147 ms |
| 2 | 194 ms | 115 ms | 134 ms | 83 ms |
| 3 | 179 ms | 96 ms | 133 ms | 83 ms |

Tracing penuh (`c895d99`, setiap request dan setiap panggilan Redis/Postgres di-span) adalah
penyebab utama regresi cache hit. Dengan tracing mati dan log penyajian aktif, p95 hit
96–149 ms, di bawah 200 ms. Sisa selisih 15–30 ms terhadap release gate belum diurai.
