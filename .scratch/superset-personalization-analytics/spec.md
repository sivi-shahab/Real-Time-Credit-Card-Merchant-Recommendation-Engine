# Spec — Dashboard Apache Superset untuk monitoring hyperpersonalisasi

Status: ready
Sumber: permintaan pengguna, 2026-09-27

## Tujuan
Product Analyst, Marketing Operator, dan ML Engineer bisa memantau seberapa personal
rekomendasi yang diterima nasabah, bagaimana nasabah meresponsnya, dan menggali insight
perilaku transaksi, lewat Apache Superset, tanpa akses langsung ke tabel operasional.

## Pertanyaan yang harus bisa dijawab
1. Berapa porsi respons yang benar-benar dipersonalisasi, dibanding cold start, cache, dan
   fallback? Bagaimana per segmen, kota, dan tier kartu?
2. Bagaimana CTR, aktivasi promo, dan redemption per posisi, per kategori, per kode alasan
   (`FAVORITE_CATEGORY`, `FREQUENT_MERCHANT`, `CARD_PROMO_ELIGIBLE`, …), dan per versi model
   dibanding baseline?
3. Bagaimana perilaku belanja bergeser: nilai dan frekuensi per kategori dari waktu ke waktu,
   merchant teratas per kota, sebaran recency dan frequency nasabah?
4. Seberapa efektif promo: layak → ditampilkan → diaktifkan → ditukar, pemakaian kuota, dan
   uplift dari holdout (bila aktif)?
5. Seberapa sehat model: kesepakatan ranking shadow vs yang disajikan, latensi, riwayat
   deployment, metrik gerbang per model?
6. Seberapa bersih data: event dikarantina per alasan dari waktu ke waktu, event terlambat?

## Kondisi sekarang dan celah data
- Tersedia di Postgres: `transaction_log`, `customers`, `merchants`, `promotions`,
  `impressions`, `interactions`, `shadow_evaluations`, `models`, `model_deployment`,
  `promo_experiment`, `uplift_reports`, `audit_events`.
- **Celah:** respons rekomendasi yang disajikan tidak dicatat di Postgres. Sumber
  (LIVE/CACHE/FALLBACK), apakah dipersonalisasi, kode alasan per item, dan promo yang
  ditampilkan hanya ada di Redis 24 jam (`served:{C}:*`, tanpa kode alasan). Pertanyaan 1, 2
  (per kode alasan), dan 4 (ditampilkan) tidak bisa dijawab tanpa log penyajian.
- Feedback di stack dev hampir kosong; data demo perlu dimuat dari dataset sintetis
  (`feedback_events.jsonl`) agar dashboard bermakna di lokal.

## Requirement
1. R1 — Log penyajian: setiap respons rekomendasi ke nasabah (bukan preview) dicatat ke
   tabel `recommendation_log` (requestId, customerId, waktu, modelVersion, source,
   personalized, city, channel) dan `recommendation_log_items` (requestId, posisi,
   merchantId, kategori, kode alasan, promotionId bila ada). Ditulis di luar jalur latensi
   (batch/async) sehingga p95 API tetap < 200 ms. Ikut dihapus oleh erasure (AC-009) dan
   punya retensi yang dikonfigurasi.
2. R2 — Skema analitik: view di skema `analytics` yang hanya mengekspos kolom yang
   dibutuhkan, tanpa `envelope` mentah, tanpa `reject_detail` bebas, dengan id nasabah
   pseudonim seperti di sistem. Agregat harian sebagai materialized view bila query berat.
3. R3 — Role Postgres `rec_analytics`: hanya `SELECT` pada skema `analytics`, tidak pada tabel
   operasional. Dibuat oleh job migrasi (pola E-4), dan untuk produksi diarahkan ke replika
   baca agar tidak membebani primary.
4. R4 — Superset di docker-compose (versi dikunci), metadata di database sendiri, terhubung
   ke Postgres sebagai `rec_analytics`.
5. R5 — Login Superset lewat Keycloak (OIDC); peran Superset dipetakan dari peran aplikasi
   (Analyst, Marketing Operator, ML Engineer, Auditor: baca; Platform Operator: admin
   Superset). Tidak ada akun lokal selain admin bootstrap.
6. R6 — Dashboard sebagai kode: dataset, chart, dan dashboard diekspor sebagai bundle YAML di
   repo dan diimpor otomatis saat start, sehingga bisa direview dan direproduksi.
7. R7 — Enam dashboard sesuai pertanyaan 1–6 di atas.
8. R8 — Data demo lokal: skrip memuat feedback dari dataset sintetis dan menjalankan trafik
   rekomendasi agar semua dashboard berisi data.
9. R9 — Privasi: tidak ada kolom yang lebih rinci dari yang sudah terlihat di dashboard
   operasional; export CSV dibatasi pada peran tertentu; akses dashboard ter-audit oleh log
   Superset.

## Di luar cakupan
Dashboard monitoring infrastruktur (itu Prometheus/Grafana, lihat
`operations-readiness`), menulis balik ke sistem dari Superset, data nasabah sungguhan.

## Rujukan
`docs/threat-model.md` (E-4, AC-009), runbook `erasure`, README bagian Signing in.
