# 04 — Dashboard sebagai kode (enam dashboard)

Status: resolved
Type: task
Owner: tim aplikasi
Blocked by: 01, 02, 03

## Yang dikerjakan
Dataset, chart, dan dashboard untuk pertanyaan 1–6 di spec, disimpan sebagai bundle YAML di
`deploy/superset/`, diimpor otomatis saat Superset start.

## Kriteria selesai
Keenam dashboard tampil dengan data dari stack lokal setelah `docker compose up`; setiap
chart menjawab pertanyaan yang tertulis di deskripsinya.

## Comments

2026-09-29 — Tetap provisioning Python (R6 diperbarui), bukan bundle YAML. Keenam dashboard
dicocokkan dengan pertanyaan 1–6 di spec; celah yang diisi: porsi personal per kota;
respons (CTR, aktivasi, penukaran) per kode alasan, kategori dan versi model; frekuensi
transaksi per kategori dari waktu ke waktu; merchant teratas per kota; sebaran recency dan
frequency; riwayat deployment (dari audit, peran saja, tanpa nama atau catatan); alasan
karantina per hari. View baru: `impression_reasons`, `customer_activity`,
`merchant_city_rank`, `model_deployment_history`. Setiap chart (53) punya deskripsi berisi
pertanyaan yang dijawabnya; dashboard menyebut nomor pertanyaannya. Chart hasil provisioning
yang tidak lagi ada di kode dihapus saat start; chart buatan pengguna (ada pemiliknya) tidak.
Dicek: 53/53 chart mengembalikan data lewat API dan tampil di browser sebagai `analyst`.
Tidak dibuat: chart uplift, karena `uplift_reports` kosong selama holdout mati
(`PROMO_HOLDOUT_PERCENT=0`); deskripsi dashboard Promo menyebutnya.

2026-09-29 — Lima chart insight ditambah (58 chart), dipilih setelah data diperiksa:
porsi belanja per desil nasabah (desil 1 = 60% belanja; kolom baru `spend_idr` dan
`value_decile` di `customer_activity`), heatmap porsi kategori per segmen, heatmap recency ×
frequency, porsi nasabah belum pernah bertransaksi (60%), dan CTR mingguan per versi model
(`baseline-1.0.0` 13% vs historis 16,6%). Tidak dibuat karena datanya datar: CTR per
segmen/tier (15,4–16,2%), belanja per hari dalam minggu, daftar nasabah bernilai tinggi yang
mulai tidak aktif (tidak ada). Chart jam transaksi ditunda: puncak di 11–12 dan 17–19 UTC,
yang cocok dengan jam makan siang/malam waktu lokal, jadi `occurred_at` kemungkinan waktu
lokal yang tersimpan sebagai UTC; perlu dicek di sumber data dulu.
Dicek: 5/5 chart baru mengembalikan data lewat ChartDataCommand; belum dilihat di browser.
