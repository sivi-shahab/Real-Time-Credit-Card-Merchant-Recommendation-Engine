# Spesifikasi SDD — Real-Time Credit Card Merchant Recommendation Engine

**Versi:** 1.0  
**Status:** Rancangan implementasi  
**Frontend:** Vue.js 3 + TypeScript  
**Backend:** Java Spring Boot, Kafka Streams, Python FastAPI  
**Data:** Kafka, PostgreSQL, Redis, object storage  
**Model:** Baseline deterministik → XGBoost Learning-to-Rank

Dokumen ini memperluas spesifikasi awal menjadi acuan implementasi dari **pembuatan data sintetis, pipeline transaksi, pengembangan model, layanan rekomendasi, hingga dashboard Vue.js**. Prototipe sebelumnya menjadi referensi alur pengguna; aplikasi produksi dibangun dengan kontrak data, otorisasi, pengujian, dan observability yang terpisah serta dapat diverifikasi.

## 1. Tujuan produk dan batas sistem
### 1.1 Tujuan
Sistem menghasilkan rekomendasi merchant dan promo berdasarkan perilaku transaksi nasabah, konteks permintaan, serta kelayakan penawaran.

Kemampuan utama:
1. Menghasilkan dataset sintetis yang konsisten dan dapat direproduksi.
2. Memproses transaksi kartu kredit melalui Kafka.
3. Menghitung profil perilaku berdasarkan histori 90 hari.
4. Menghasilkan kandidat merchant yang relevan.
5. Memfilter promo berdasarkan aturan bisnis.
6. Melakukan ranking dan menyajikan hasil melalui API.
7. Menyediakan dashboard Vue.js untuk eksplorasi, operasi, evaluasi, dan audit.
8. Merekam feedback untuk pelatihan dan evaluasi model berikutnya.

### 1.2 Ruang lingkup
| Termasuk | Tidak termasuk |
|---|---|
| Transaksi sintetis dan simulator streaming | Pemrosesan pembayaran |
| Customer profiling | Credit scoring |
| Merchant dan promotion intelligence | Fraud detection |
| Candidate generation dan ranking | Persetujuan kartu kredit |
| Recommendation API | Penyimpanan PAN/CVV |
| Feedback impression, klik, dan redemption | Settlement pembayaran |
| Dashboard operasional Vue.js | Penggantian sistem CRM utama |
| Model training dan evaluasi | Klaim kepatuhan otomatis |

### 1.3 Pengguna
| Peran | Kebutuhan utama |
|---|---|
| Product Analyst | Mengukur relevansi, coverage, dan konversi |
| Marketing Operator | Mengelola promo dan segmentasi kelayakan |
| ML Engineer | Mengelola dataset, eksperimen, evaluasi, dan versi model |
| Platform Operator | Memantau Kafka, API, feature freshness, dan kegagalan |
| Auditor | Membaca perubahan konfigurasi serta aktivitas administratif |
| Nasabah | Menerima rekomendasi melalui kanal mobile banking |

Dashboard administratif dan API nasabah memiliki scope akses berbeda.

## 2. Prinsip SDD dan artefak wajib
Setiap kapabilitas harus memiliki ID requirement, kontrak input/output, invariant dan aturan bisnis, skenario normal serta kegagalan, acceptance criteria, pengujian otomatis, metrik operasional, dan pemilik komponen.

### 2.1 Artefak spesifikasi
| Artefak | Isi |
|---|---|
| Product specification | Tujuan, pengguna, ruang lingkup, KPI |
| Domain specification | Entitas, lifecycle, invariant |
| OpenAPI | Kontrak REST frontend/backend dan serving |
| AsyncAPI | Topic, producer, consumer, event flow |
| Avro schemas | Struktur event dan evolusi schema |
| Feature specification | Definisi, window, normalisasi, versi |
| Model specification | Label, sampling, training, evaluasi |
| UI specification | Halaman, komponen, interaksi, akses |
| Test specification | Unit, integration, E2E, load, recovery |
| ADR | Alasan keputusan arsitektur penting |
| Runbook | Deploy, rollback, replay, incident, restore |

**Definition of Ready:** requirement tidak masuk implementasi sebelum kontrak, aturan bisnis, dan acceptance criteria disepakati.

**Definition of Done:** kode, migrasi, pengujian, dokumentasi, metrik, dan runbook terkait tersedia.

## 3. Arsitektur target
### 3.1 Alur utama
**Data sintetis / CC source → ingestion → Kafka → normalisasi → feature engine → feature store → candidate generation → ranking → serving store → API → Vue.js/mobile banking.**

Alur pendukung:
- Merchant dan promo → PostgreSQL → outbox/CDC → Kafka → materialized cache.
- Impression dan interaction → Kafka → analytical storage → training dataset.
- Training → MLflow → model registry → deployment inference.
- Service telemetry → OpenTelemetry/Prometheus → dashboard operasional.

### 3.2 Komponen
| Komponen | Teknologi | Tanggung jawab |
|---|---|---|
| Synthetic Data Generator | Python | Membuat dataset reproducible |
| Transaction Simulator | Python atau Java | Replay transaksi dengan kontrol kecepatan |
| Ingestion Service | Spring Boot | Validasi dan publikasi transaksi |
| Feature Engine | Kafka Streams | Normalisasi, deduplikasi, agregasi |
| Merchant/Promotion Service | Spring Boot + PostgreSQL | Master data dan aturan promo |
| Candidate Service | Spring Boot | Retrieval dan filtering kandidat |
| Ranking Service | FastAPI + XGBoost | Batch inference kandidat |
| Recommendation Service | Spring Boot | Orkestrasi, fallback, cache, API |
| Feedback Service | Spring Boot | Impression, klik, redemption |
| Training Pipeline | Python | Dataset, training, evaluasi |
| Dashboard BFF | Spring Boot | API administratif dan agregasi dashboard |
| Dashboard | Vue.js 3 | Antarmuka pengguna internal |
| Object Storage | S3-compatible | Raw data, Parquet, dataset dan artefak |
| Online Store | Redis | Fitur online dan recommendation cache |
| Registry | Schema Registry + MLflow | Schema event dan lifecycle model |

BFF dapat menjadi modul deployment Recommendation Service pada tahap awal, dengan kontrak dan otorisasi tetap terpisah.

### 3.3 Sumber kebenaran data
- PostgreSQL: konfigurasi merchant, promo, job, dan audit administratif.
- Raw event archive: histori event untuk replay dan rekonstruksi.
- Kafka: transport event dengan retensi terbatas.
- Redis: penyimpanan online yang dapat dibangun ulang.
- Model registry: versi model yang disetujui untuk deployment.

Redis tidak menjadi satu-satunya penyimpanan histori.

## 4. Spesifikasi pembuatan data sintetis
### SYN-001 — Dataset reproducible
| Parameter | Contoh | Ketentuan |
|---|---|---|
| seed | 42 | Seed sama menghasilkan dataset sama |
| referenceTime | 2026-09-23T00:00:00Z | Acuan waktu eksplisit |
| customerCount | 10.000 | Dapat dikonfigurasi |
| merchantCount | 1.000 | Dapat dikonfigurasi |
| promotionCount | 100 | Dapat dikonfigurasi |
| transactionCount | 1.000.000 | Target volume |
| historyDays | 180 | Mendukung training dan backtesting |
| currency | IDR | MVP satu mata uang |
| outputFormat | Parquet + JSONL | Analitik dan replay |
| scenarioProfile | normal/mixed/failure | Komposisi skenario |

Waktu tidak boleh bergantung pada jam mesin secara implisit untuk proses dataset statis.

### SYN-002 — Entitas sintetis
**Customer:** customerId berupa surrogate ID, cityCode, cardTier, syntheticSegment, preferredCategories, spendingBand, activityPattern, personalizationAllowed.

Tidak menghasilkan nama nyata, nomor telepon, alamat lengkap, PAN, atau CVV.

**Merchant:** merchantId, merchantName, categoryCode, cityCode, channel (ONLINE/OFFLINE), rating, status, latitude/longitude sintetis bila diperlukan.

**Promotion:** promotionId, merchantId, benefitType, benefitValue, minSpendMinor, maxBenefitMinor, eligibleCardTiers, eligibleCityCodes, startsAt, endsAt, campaignQuota, perCustomerLimit, status.

**Transaction:** transactionId, customerId, merchantId, transactionType, originalTransactionId untuk refund/reversal, amountMinor, currency, occurredAt, sourceSequence bila tersedia.

### SYN-003 — Distribusi perilaku
- Popularitas merchant mengikuti distribusi berekor panjang.
- Nilai transaksi menggunakan distribusi positif yang berbeda per kategori.
- Aktivitas mengikuti pola jam, hari kerja, dan akhir pekan.
- Nasabah memiliki preferensi kategori dan lokasi yang tidak identik.
- Sebagian nasabah berpindah preferensi untuk skenario drift.
- Nasabah baru memiliki histori kosong atau terbatas.
- Promo dapat meningkatkan peluang interaksi pada segmen tertentu.

Parameter distribusi disimpan di manifest; semuanya adalah asumsi simulasi, bukan estimasi perilaku nasabah nyata.

### SYN-004 — Dataset feedback
Generator menyediakan recommendation request, candidate set, item yang ditampilkan dan posisinya, impression, click, promo activation, redemption, dan purchase teratribusi.

Outcome dibuat secara probabilistik dari preferensi laten, sensitivitas promo, position bias, dan noise. Formula outcome tidak boleh menyalin formula baseline ranking secara langsung.

**Batas evaluasi:** performa pada data sintetis membuktikan integritas pipeline dan perilaku eksperimen; tidak membuktikan uplift bisnis nyata.

### SYN-005 — Skenario kualitas dan kegagalan
| Skenario | Hasil yang diharapkan |
|---|---|
| Duplicate eventId | Tidak menghasilkan efek kedua |
| Duplicate transactionId | Tidak menambah agregat kembali |
| Amount nol/negatif | Ditolak |
| Refund melebihi sisa nilai asli | Dikarantina |
| Customer tidak dikenal | Dikarantina sesuai kebijakan referensi |
| Merchant tidak dikenal | Masuk pending enrichment |
| Schema tidak kompatibel | Gagal validasi |
| Event terlambat | Diproses sesuai late-event policy |
| Timestamp terlalu jauh di masa depan | Dikarantina |
| Promo kedaluwarsa | Tidak ditawarkan |
| Kuota promo habis | Tidak ditawarkan |
| Personalisasi tidak diizinkan | Tidak menggunakan fitur perilaku |

Persentase injeksi setiap skenario dapat dikonfigurasi.

### SYN-006 — Output dataset
Satu dataset menghasilkan tabel customer, merchant, promotion, transaction; feedback events; manifest konfigurasi; versi generator dan schema; checksum tiap berkas; laporan kualitas data; ringkasan distribusi; serta event replay file.

**Acceptance criteria:** dua eksekusi dengan konfigurasi, seed, referenceTime, dan versi generator yang sama menghasilkan checksum identik.

## 5. Simulator streaming
### SIM-001 — Kontrol replay
Simulator mendukung start, pause, resume, stop; target TPS; burst multiplier; replay berdasarkan event time; mode dipercepat; pemilihan dataset dan rentang waktu; failure injection; progress dan statistik publikasi.

TPS aktual dibedakan dari TPS target.

### SIM-002 — Checkpoint dan retry
Checkpoint menyimpan datasetId, runId, offset, dan status. Saat restart, simulator melanjutkan dari checkpoint. Replay mempertahankan identitas transaksi dan event bisnis agar deduplikasi downstream dapat diverifikasi. Menjalankan dataset sebagai eksperimen independen menggunakan namespace ID baru.

### SIM-003 — Batas operasional
- Simulator hanya aktif di development/staging secara default.
- Akses dikontrol oleh role.
- Rate limit maksimum ditentukan administrator.
- Stop menghentikan publikasi baru; pesan yang sudah berada di Kafka tetap dapat diproses.

## 6. Model domain dan invariant transaksi
### 6.1 Representasi nilai uang
Gunakan integer `amountMinor` dengan aturan minor unit berdasarkan mata uang. MVP hanya IDR; nilai 150000 merepresentasikan IDR 150.000. Float tidak digunakan untuk akumulasi uang.

### 6.2 Lifecycle transaksi
Jenis event: PURCHASE, REFUND, REVERSAL. Semua amount positif; jenis transaksi menentukan dampak terhadap net spending.

1. Refund/reversal harus menunjuk transaksi asli.
2. Total refund tidak boleh melebihi nilai asli.
3. Reversal penuh tidak boleh dihitung kembali sebagai refund tambahan.
4. Net monetary tidak boleh negatif pada level transaksi asli.
5. Transaksi yang dibatalkan penuh tidak berkontribusi pada frequency.
6. Partial refund mengurangi monetary tanpa menambah frequency.
7. Koreksi kategori mengikuti kategori transaksi asli.

Agregasi bersifat eventual consistent. Rekonsiliasi periodik membandingkan fitur online dengan hasil recomputation offline.

## 7. Kontrak event dan Kafka
### EVT-001 — Envelope standar
| Field | Tipe | Keterangan |
|---|---|---|
| eventId | UUID string | Identitas unik event |
| eventType | enum/string terkontrol | Jenis event |
| eventVersion | string | Versi semantik kontrak |
| occurredAt | timestamp-millis UTC | Waktu kejadian bisnis |
| producedAt | timestamp-millis UTC | Waktu publikasi |
| producer | string | Identitas layanan |
| traceparent | nullable string | Distributed tracing |
| correlationId | nullable string | Korelasi proses |
| payload | typed Avro record | Sesuai topic/schema |

Payload transaksi menggunakan typed record, bukan object bebas.

### EVT-002 — Topic
| Topic | Key | Producer | Consumer utama |
|---|---|---|---|
| cc.transactions | customerId | Ingestion | Transaction normalizer |
| cc.transactions.normalized | customerId | Normalizer | Feature Engine |
| merchant.catalog | merchantId | Merchant Service | Candidate Service |
| promotion.events | promotionId | Promotion Service | Eligibility/serving |
| customer.features | customerId | Feature Engine | Online store updater |
| recommendations | customerId | Recommendation worker | Serving materializer |
| recommendation.impressions | customerId | Feedback Service | Analytics/training |
| recommendation.interactions | customerId | Feedback Service | Attribution/training |
| pipeline.dlq | source key | Pipeline services | Operations/recovery |

`recommendations` menjadi event internal. Mobile banking mengakses REST API, bukan Kafka.

### EVT-003 — Delivery semantics
- Producer idempotent dan `acks=all`.
- Replication factor 3 dan minimum in-sync replicas 2 untuk produksi.
- Kafka Streams menggunakan `exactly_once_v2` pada transaksi Kafka yang relevan.
- Redis sink tetap memerlukan conditional update dan idempotency.
- Deduplikasi menggunakan eventId serta identitas transaksi bisnis.
- Feature update membawa version/sequence untuk mencegah overwrite oleh update lama.

Penambahan partition pada topic keyed memerlukan rencana migrasi atau rebuild state karena perubahan pemetaan key dapat memengaruhi ordering dan state locality.

### EVT-004 — Schema evolution
- Registry menerapkan `BACKWARD_TRANSITIVE`.
- Field baru harus memiliki default yang sesuai.
- Perubahan makna atau unit field tidak dilakukan secara diam-diam.
- Breaking change menggunakan kontrak baru dan migrasi consumer.
- Compatibility test berjalan dalam CI.

### EVT-005 — Retensi awal
Nilai awal untuk estimasi kapasitas: raw transaction topic 7 hari; feedback topic 14 hari; DLQ 14 hari; latest-state topics menggunakan compaction; raw archive sintetis nonproduksi 180 hari.

Retensi produksi mengikuti kebutuhan replay, volume, persetujuan legal, dan kebijakan penghapusan data.

## 8. Feature engineering
### FEAT-001 — Jendela perilaku
Fitur dihitung untuk 1, 7, 30, dan 90 hari.

| Fitur | Definisi |
|---|---|
| transactionCount90d | Jumlah purchase efektif |
| netSpend90d | Nilai setelah refund/reversal |
| averageSpend90d | Net spend dibagi purchase efektif |
| categoryFrequencyShare | Proporsi frekuensi per kategori |
| categoryMonetaryShare | Proporsi monetary per kategori |
| categoryRecency | Peluruhan sejak transaksi kategori terakhir |
| merchantAffinity | Frekuensi dan recency merchant |
| preferredCity | Kota dengan aktivitas tertinggi |
| activeHourDistribution | Distribusi waktu lokal |
| daysSinceLastTransaction | Recency keseluruhan |
| historyDepthDays | Kedalaman histori |
| coldStartFlag | Histori tidak mencukupi |

### FEAT-002 — Formula minat kategori
$$
I_{u,c}=0.4F_{u,c}+0.3M_{u,c}+0.3R_{u,c}
$$

Dengan:

$$
F_{u,c}=\frac{N_{u,c}}{\max(N_u,1)}
$$

$$
M_{u,c}=\frac{V_{u,c}}{\max(V_u,1)}
$$

$$
R_{u,c}=\exp\left(-\frac{\Delta t_{u,c}}{30}\right)
$$

Kategori tanpa histori memiliki recency nol. Monetary menggunakan net spending nonnegatif. Bobot dapat dikonfigurasi, harus nonnegatif, dan dinormalisasi menjadi total satu.

### FEAT-003 — Expiration dan late events
- Agregasi menggunakan event time.
- Bucket harian menyimpan histori yang cukup untuk jendela 90 hari.
- Scheduler menghapus kontribusi yang keluar dari window meskipun tidak ada transaksi baru.
- Recency dihitung terhadap feature as-of time.
- Late arrival sampai 24 jam diproses melalui jalur koreksi normal.
- Event lebih terlambat diarahkan ke correction/backfill.
- Event lama tetap masuk archive untuk audit dan rekonsiliasi.

Batas 24 jam adalah konfigurasi awal yang harus dikonfirmasi dengan karakteristik sumber transaksi.

### FEAT-004 — Feature store contract
Feature snapshot memuat customerId, featureSchemaVersion, featureVersion, computedAt, featureAsOf, lastEventOccurredAt, sourceCheckpoint, dan feature values.

Training menggunakan point-in-time join. Fitur masa depan tidak boleh masuk ke contoh training masa lalu.

## 9. Candidate generation dan promo matching
### CAND-001 — Sumber kandidat
Gabungkan kandidat dari kategori favorit, merchant affinity, merchant populer menurut kota/kategori, merchant dengan promo eligible, dan kandidat eksplorasi.

Default awal:
- Maksimal 200 kandidat sebelum ranking.
- Maksimal 20 hasil API.
- Default 10 hasil.

Deduplicate berdasarkan merchantId.

### CAND-002 — Hard filtering
Hapus kandidat jika merchant tidak aktif, kanal merchant tidak sesuai, merchant masuk exclusion list, tidak memenuhi batas wilayah yang berlaku, atau kategori dilarang oleh kebijakan produk.

### PROMO-001 — Kelayakan promo
Evaluasi status dan periode promo, card tier, kota dan kanal, minimum transaksi, batas per nasabah, kuota kampanye, dan persyaratan merchant.

Jika nilai transaksi berikutnya belum diketahui, promo ditampilkan dengan syarat minimum belanja; sistem tidak mengubah status syarat tersebut menjadi terpenuhi.

Pemeriksaan kuota pada rekomendasi tidak menjamin redemption. Reservasi atau konsumsi kuota dilakukan secara atomik oleh layanan redemption yang berwenang.

### CAND-003 — Cold start
Nasabah tanpa histori mendapat rekomendasi berbasis kota yang tersedia dan diizinkan, popularitas merchant, rating, kelayakan kartu/promo, dan diversifikasi kategori.

Nasabah yang tidak mengizinkan personalisasi tidak memakai fitur perilaku.

## 10. Model ranking dan training
### ML-001 — Baseline
Baseline awal menggunakan 65% minat kategori, 20% kecocokan lokasi, 10% rating ternormalisasi, dan 5% kelayakan promo. Seluruh komponen dinormalisasi. Skor baseline adalah skor relevansi; bukan probabilitas klik.

### ML-002 — Learning-to-Rank
Model target: XGBoost dengan objective ranking, misalnya `rank:ndcg`.

Satu kelompok training mewakili satu recommendation request. Fitur mencakup customer features, merchant features, customer–merchant affinity, promo features, konteks lokasi dan waktu, serta metadata ketersediaan fitur.

### ML-003 — Label
| Outcome | Relevance label |
|---|---:|
| Impression tanpa interaksi dalam observation window | 0 |
| Click | 1 |
| Promo activation | 2 |
| Qualified purchase/redemption | 3 |

Jika terdapat beberapa outcome, gunakan label tertinggi yang valid. Contoh hanya boleh dianggap negatif setelah observation window selesai. Kandidat yang tidak ditampilkan tidak otomatis menjadi negatif.

### ML-004 — Attribution
- requestId menghubungkan hasil serving dengan exposure.
- impressionId dibuat ketika item benar-benar terlihat di kanal.
- Klik merujuk impressionId.
- Jendela atribusi awal: 7 hari.
- Satu conversion memiliki conversionId unik dan satu atribusi utama.
- Aturan atribusi dicatat sebagai attributionPolicyVersion.

Aturan last-touch dapat digunakan untuk pelaporan awal, tetapi uplift kausal diukur melalui eksperimen terkontrol.

### ML-005 — Pipeline training
1. Pilih dataset snapshot.
2. Validasi schema dan kualitas.
3. Bentuk exposure dan outcome.
4. Lakukan point-in-time feature join.
5. Bentuk request groups.
6. Pisahkan data secara temporal.
7. Berikan gap sesuai observation window.
8. Train baseline pembanding dan model kandidat.
9. Evaluasi per segmen.
10. Simpan artefak, konfigurasi, dan dataset lineage.
11. Ajukan model untuk persetujuan.
12. Jalankan shadow/canary sebelum promotion.

### ML-006 — Gerbang evaluasi
Metrik: NDCG@10, Recall@10 candidate retrieval, merchant coverage, category diversity, latensi inference, missing feature rate, serta kinerja per kelompok histori dan kota.

Kriteria awal model kandidat:
- NDCG@10 tidak lebih buruk dari baseline pada holdout.
- Tidak ada penurunan kritis pada segmen yang disepakati.
- Memenuhi latency budget.
- Feature schema kompatibel.
- Artefak training dan evaluasi lengkap.

Promosi model memerlukan persetujuan; skor offline tinggi saja tidak cukup.
## 11. Recommendation serving
### SERV-001 — Alur request
1. Validasi token dan hak akses customer.
2. Periksa cache dengan konteks serta versi yang sesuai.
3. Ambil fitur online.
4. Generate dan filter kandidat.
5. Evaluasi promo.
6. Batch inference.
7. Terapkan diversifikasi dan aturan bisnis.
8. Simpan hasil/cache.
9. Kembalikan respons dan metadata.

Untuk request nasabah, identitas di token harus cocok dengan resource customer. Akses administratif menggunakan endpoint dan scope terpisah.

### SERV-002 — Respons
Response memuat requestId, customerId, generatedAt, featureAsOf, modelVersion, featureSchemaVersion, rankingConfigVersion, source (CACHE/LIVE/FALLBACK), stale, dan recommendations.

Setiap rekomendasi memuat merchantId, merchantName, rank, score, scoreType (RELATIVE_RELEVANCE), reasonCodes, promo eligible beserta syarat, dan tracking token bila digunakan.

Reason code menggunakan kode stabil, misalnya `FAVORITE_CATEGORY`, `LOCAL_MERCHANT`, dan `CARD_PROMO_ELIGIBLE`.

### SERV-003 — Fallback
Jika inference gagal:
1. Gunakan baseline yang tersedia.
2. Jika fitur tidak tersedia, gunakan cold-start ranking.
3. Jika dependency tetap gagal, gunakan daftar populer yang masih valid.
4. Jika tidak ada kandidat aman, kembalikan daftar kosong dengan reason code.

Promo tetap harus lolos validasi masa berlaku saat serving.

### SERV-004 — Cache dan invalidasi
Key cache mencakup customer, context, model version, dan ranking configuration version.

TTL awal: 5 menit, dengan refresh/invalidation saat fitur berubah, merchant dinonaktifkan, promo berubah atau kedaluwarsa, atau model/config dipromosikan.

TTL fitur harus cukup untuk mempertahankan state nasabah pasif; penghapusan fitur tidak boleh menjadi mekanisme expiration window.

## 12. Spesifikasi API
Semua API menggunakan OpenAPI 3.1, validasi input, correlation ID, dan error format konsisten.

| Method | Endpoint | Fungsi |
|---|---|---|
| GET | `/api/v1/customer/{id}/recommendations` | Rekomendasi kanal nasabah |
| POST | `/api/v1/feedback/impressions` | Merekam exposure |
| POST | `/api/v1/feedback/interactions` | Klik/aktivasi |
| POST | `/admin/v1/datasets` | Membuat job dataset sintetis |
| GET | `/admin/v1/datasets/{id}` | Status, manifest, hasil |
| POST | `/admin/v1/simulations` | Memulai simulasi |
| POST | `/admin/v1/simulations/{id}/pause` | Pause |
| POST | `/admin/v1/simulations/{id}/resume` | Resume |
| POST | `/admin/v1/simulations/{id}/stop` | Stop |
| GET | `/admin/v1/transactions` | Penelusuran transaksi |
| GET | `/admin/v1/customers/{id}/features` | Profil terotorisasi |
| POST | `/admin/v1/recommendations/preview` | Simulasi rekomendasi internal |
| GET/POST/PATCH | `/admin/v1/merchants` | Master merchant |
| GET/POST/PATCH | `/admin/v1/promotions` | Master promo |
| POST | `/admin/v1/training-jobs` | Menjalankan training |
| GET | `/admin/v1/models` | Daftar model |
| POST | `/admin/v1/models/{id}/promote` | Persetujuan deployment |
| POST | `/admin/v1/models/{id}/rollback` | Rollback |
| GET | `/admin/v1/metrics/overview` | Metrik agregat |
| GET | `/admin/v1/audit-events` | Audit trail |
| GET | `/admin/v1/events` | SSE status job/operasi |

Untuk PATCH, resource ID diletakkan pada path item, misalnya `/promotions/{id}`.

### 12.1 Aturan kontrak
- Timestamp menggunakan UTC dengan offset eksplisit.
- Pagination menggunakan cursor untuk log besar.
- List memiliki batas maksimum halaman.
- Job panjang mengembalikan HTTP 202 dan jobId.
- Operasi penciptaan job mendukung `Idempotency-Key`.
- Update resource menggunakan version/ETag untuk konflik concurrent.
- Error memuat code, message, fieldErrors, traceId.
- Rate limiting mengembalikan HTTP 429.
- Otorisasi ditolak dengan HTTP 403; frontend tidak menjadi enforcement utama.

## 13. Dashboard Vue.js
### 13.1 Stack frontend
- Vue.js 3 Composition API.
- TypeScript strict.
- Vite.
- Vue Router.
- Pinia untuk state UI/session.
- TanStack Query untuk server state.
- PrimeVue untuk komponen.
- Apache ECharts untuk visualisasi.
- VeeValidate + Zod untuk form validation.
- Vitest + Vue Test Utils.
- Playwright untuk E2E.

Pemilihan versi dependency dipatok melalui lockfile dan kebijakan pembaruan.

### 13.2 Struktur modul
Authentication; Overview; Synthetic datasets; Simulations; Transactions; Customer profiles; Recommendation explorer; Merchants; Promotions; Models and experiments; Monitoring; Audit and settings.

### UI-001 — Overview
Menampilkan throughput aktual, API p50/p95/p99, ranking latency, event-to-serving freshness, consumer lag, active model, cache hit rate, fallback rate, error rate, dan recommendation coverage.

Setiap panel menunjukkan rentang waktu, satuan, dan waktu refresh terakhir. Metrik unavailable tidak ditampilkan sebagai nol.

### UI-002 — Synthetic Data Studio
Form: seed, reference time, jumlah entity, histori hari, distribusi kategori, segment mix, besaran failure injection, format output.

Interaksi:
- Validasi konfigurasi.
- Tampilkan estimasi ukuran sebelum submit.
- Jalankan job.
- Lihat progress.
- Unduh manifest dan quality report.
- Buka detail distribusi.
- Pilih dataset untuk replay.

### UI-003 — Transaction Simulator
Menampilkan kontrol start/pause/resume/stop, target TPS, actual TPS, sent/failed count, checkpoint, dan remaining events.

Tombol mengikuti state machine: **CREATED → RUNNING ↔ PAUSED → COMPLETED/STOPPED/FAILED.**

Perintah yang tidak valid untuk status saat ini harus ditolak backend.

### UI-004 — Transaction Explorer
Filter: customer, merchant, waktu kejadian, waktu diterima, jenis transaksi, status validasi, correlation/transaction ID.

Detail menampilkan raw envelope yang sudah direduksi dari data sensitif, processing timeline, dan alasan quarantine. Replay DLQ memerlukan izin dan konfirmasi; semua replay diaudit.

### UI-005 — Customer Profile
Menampilkan histori 7/30/90 hari, category interest, merchant affinity, average spend, lokasi pilihan, active time distribution, feature version, freshness, dan status izin personalisasi.

Akses profil nasabah dicatat dalam audit. Pencarian massal dan ekspor dibatasi.

### UI-006 — Recommendation Explorer
Pengguna memilih customer, lokasi/konteks, limit, dan model yang diizinkan.

Hasil menunjukkan daftar kandidat, alasan kandidat dibuang, ranking akhir, komponen baseline atau penjelasan model yang tersedia, promo eligible dan syarat, latensi tiap tahap, metadata versi, dan respons JSON.

Mode preview tidak mencatat impression bisnis dan tidak menimpa cache produksi.

### UI-007 — Merchant dan Promo
Mendukung search/filter, create/edit, aktivasi/nonaktivasi, preview eligibility, validasi periode, simulasi minimum belanja dan tier, serta riwayat perubahan.

Perubahan promo kritis dapat memakai alur draft → review → approved → active.

### UI-008 — Model dan Eksperimen
Menampilkan model version, dataset snapshot, feature schema, training parameters, metrik holdout, perbandingan baseline, segment performance, deployment status, dan approval history.

Tombol promote/rollback hanya terlihat untuk role terkait, dengan pemeriksaan ulang di backend.

### UI-009 — Monitoring dan Audit
Monitoring menyediakan drill-down dari service → trace → error, tanpa mengekspos payload sensitif.

Audit mencatat actor, action, resource, waktu, perubahan, dan outcome. Audit tidak dapat diedit melalui dashboard.

### 13.3 Perilaku lintas halaman
- Filter utama tersimpan di URL.
- Request pencarian diberi debounce.
- Pagination server-side.
- Request lama dibatalkan saat filter berubah.
- Tersedia loading, empty, error, stale, dan forbidden state.
- SSE memiliki reconnect/backoff dan polling fallback.
- Konfirmasi diperlukan untuk operasi destruktif.
- Form mencegah submit ganda.
- Dashboard dapat digunakan dengan keyboard.
- Grafik memiliki ringkasan tekstual.
- Angka/uang/tanggal ditampilkan dengan locale Indonesia.

### 13.4 Autentikasi browser
Gunakan BFF dengan session cookie `HttpOnly`, `Secure`, serta kebijakan `SameSite` yang sesuai; BFF menyimpan token upstream.

Request mutasi dilindungi dari CSRF. Token akses tidak disimpan di localStorage.
## 14. Keamanan, privasi, dan audit
### SEC-001 — Kontrol akses
Role minimum: Viewer, Analyst, Marketing Operator, ML Engineer, Platform Operator, Approver, Auditor.

Hak istimewa menggunakan least privilege. Persetujuan model/promo kritis dapat mewajibkan pemisahan pembuat dan penyetuju.

### SEC-002 — Perlindungan data
- TLS eksternal dan internal.
- SASL/TLS serta ACL Kafka.
- Encryption at rest.
- Secret manager.
- Key rotation.
- Redaksi log.
- Pseudonymous customer ID.
- Pembatasan ekspor.
- Retensi dan penghapusan terkoordinasi.

Penghapusan mencakup cache, feature store, arsip/dataset sesuai kebijakan, dan pencegahan data terhapus muncul kembali saat replay.

### SEC-003 — Kepatuhan
PCI DSS dan UU PDP diperlakukan sebagai workstream dengan penilaian ruang lingkup, dokumentasi kontrol, serta validasi pihak yang berwenang. Tidak ada klaim “compliant” hanya karena teknologi enkripsi atau OAuth2 sudah digunakan.

## 15. Non-functional requirements
| Area | Target awal |
|---|---|
| Ingestion steady throughput | 10.000 transaksi/detik |
| Load test burst | 20.000 transaksi/detik selama 5 menit |
| API recommendation | p95 <200 ms pada profil trafik yang disepakati |
| On-demand ranking path | p95 <500 ms |
| Event-to-serving freshness | p95 <5 detik pada beban normal |
| Availability API | 99,9% per bulan |
| Initial Vue dashboard load | p75 <3 detik pada lingkungan uji yang didefinisikan |
| Dashboard refresh | 5–15 detik sesuai panel |
| Candidate count | Maksimal 200 per request |
| Recommendation output | Default 10, maksimal 20 |

API <200 ms dicapai terutama melalui materialized recommendations/cache. Fresh recomputation tidak boleh diasumsikan memiliki budget yang sama tanpa pengujian.

Pengujian 100.000 transaksi/menit setara sekitar 1.667 transaksi/detik, sehingga belum memvalidasi target 10.000 TPS.

Pada 10.000 TPS selama sehari, volume mencapai 864 juta transaksi. Estimasi kapasitas wajib memasukkan ukuran event, replikasi, index, changelog, retensi, dan biaya archive.

## 16. Spesifikasi pengujian
| Level | Pengujian wajib |
|---|---|
| Generator | Reproducibility, referential integrity, distribusi |
| Unit | Formula fitur, eligibility, refund, ranking, validation |
| Contract | OpenAPI, Avro compatibility, producer/consumer |
| Integration | Kafka → fitur → Redis → ranking → API |
| Frontend component | Form, permission state, table, error handling |
| E2E | Generate dataset → replay → inspect profile → recommendation |
| ML | Temporal split, leakage test, feature parity, reproducibility |
| Performance | Throughput, API latency, inference, dashboard query |
| Resilience | Consumer restart, broker loss, Redis loss, model timeout |
| Security | IDOR/BOLA, RBAC, CSRF, injection, secret leakage |
| Recovery | Replay, rebuild state, restore database, rollback model |

### 16.1 Acceptance scenarios kritis
**AC-001 — Deduplikasi:** Given transaksi valid sudah diproses, when event dikirim ulang, then transaction count dan spending tidak berubah.

**AC-002 — Refund:** Given transaksi IDR 150.000, when refund IDR 50.000 valid diterima, then kontribusi monetary menjadi IDR 100.000 tanpa penambahan frekuensi.

**AC-003 — Window expiration:** Given transaksi keluar dari jendela 90 hari, when waktu evaluasi bergeser tanpa event baru, then kontribusinya tetap dikeluarkan.

**AC-004 — Fallback:** Given model service timeout, when rekomendasi diminta, then baseline/fallback dikembalikan dengan metadata yang tepat.

**AC-005 — Promo expiry:** Given cache mengandung promo yang sudah kedaluwarsa, when respons disajikan, then promo tersebut tidak ditampilkan sebagai aktif.

**AC-006 — Otorisasi:** Given token nasabah C001, when endpoint rekomendasi C002 diakses, then sistem menolak permintaan.

**AC-007 — Dashboard isolation:** Given analyst menjalankan preview, then tidak terbentuk impression bisnis dan cache produksi tidak berubah.

**AC-008 — Rekonsiliasi:** Given replay dataset selesai, then hasil agregasi online sesuai hasil recomputation offline untuk dataset acuan.

**AC-009 — Data deletion:** Given penghapusan nasabah telah disetujui, when archive direplay, then data yang terhapus tidak dimaterialisasikan kembali.

## 17. Deployment dan CI/CD
### 17.1 Lingkungan
**Local:** Docker Compose untuk Kafka, registry, PostgreSQL, Redis, object storage, backend, dan Vue.js.

**Staging:** Kubernetes dengan konfigurasi representatif, dataset sintetis, serta integrasi observability.

**Production:** deployment multi-zone, resource requests/limits, HPA, disruption budgets, network policies, dan backup.

Kafka/PostgreSQL/Redis dikelola sebagai layanan stateful dengan prosedur backup/recovery yang eksplisit, atau memakai managed services.

### 17.2 Pipeline
1. Lint dan type checking.
2. Unit/component tests.
3. Contract compatibility.
4. Integration tests.
5. Build artefak.
6. Dependency/SAST/container scanning.
7. Generate SBOM.
8. Sign image.
9. Deploy staging.
10. Smoke dan E2E.
11. Persetujuan.
12. Canary production.
13. Validasi SLO.
14. Rollback otomatis jika guardrail gagal.

Migrasi database menggunakan pola expand–migrate–contract.

Model, feature schema, dan ranking config memiliki versi independen tetapi compatibility matrix harus tersedia.

## 18. Tahapan implementasi
### Fase 0 — Kontrak dan fondasi
Deliverable: spesifikasi disetujui, domain model, OpenAPI/AsyncAPI/Avro, ADR, repository dan CI dasar, threat model.

**Exit criterion:** contract tests dasar berjalan.

### Fase 1 — Data sintetis dan replay
Deliverable: generator, dataset manifest, quality report, simulator, halaman Data Studio dan Simulator Vue.js.

**Exit criterion:** dataset reproducible dan replay dapat dilanjutkan dari checkpoint.

### Fase 2 — Streaming dan fitur
Deliverable: normalizer, deduplikasi, refund/reversal handling, rolling features, Redis materialization, Transaction Explorer dan Customer Profile.

**Exit criterion:** hasil online sesuai offline oracle, termasuk duplicate dan late events.

### Fase 3 — Baseline recommendation
Deliverable: candidate generation, promo eligibility, baseline ranking, cache/fallback, API, Recommendation Explorer.

**Exit criterion:** alur ujung ke ujung berjalan tanpa model ML.

### Fase 4 — Feedback dan ML
Deliverable: impression/interaction collection, attribution, training dataset, XGBoost pipeline, MLflow integration, model dashboard.

**Exit criterion:** training reproducible, evaluasi temporal, dan deployment shadow tersedia.

### Fase 5 — Production hardening
Deliverable: SSO/RBAC, audit, load/resilience/security tests, observability, canary/rollback, backup/restore, operational runbooks.

**Exit criterion:** gerbang keamanan, performa, pemulihan, dan operasional disetujui sebelum trafik nasabah nyata.

## 19. Definition of Done aplikasi keseluruhan
Aplikasi dianggap selesai untuk rilis yang disepakati ketika:
- Dataset sintetis dapat dibuat, diverifikasi, dan direplay dari dashboard.
- Event invalid tidak mencemari feature store.
- Deduplikasi, refund, late arrival, serta expiration window teruji.
- Fitur online memiliki lineage dan hasil rekonsiliasi.
- Rekomendasi tetap tersedia saat model gagal.
- Promo diperiksa kembali saat serving.
- Model training tidak mengalami leakage waktu.
- Dashboard Vue.js menampilkan data backend aktual, bukan angka statis.
- Otorisasi enforced pada backend.
- Seluruh operasi administratif penting diaudit.
- Target beban dan latency memiliki hasil uji.
- Restore, replay, dan rollback sudah dipraktikkan.
- Batas penggunaan data, retensi, dan persetujuan personalisasi terdokumentasi.

**Urutan pengerjaan yang dianjurkan adalah menyelesaikan alur data sintetis → streaming → fitur → baseline → API → dashboard terlebih dahulu, kemudian menambahkan ML ranking.** Dengan urutan ini, integritas data dan operasional sudah dapat diuji sebelum kualitas model menjadi faktor tambahan.