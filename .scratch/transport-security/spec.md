# Spec — Keamanan transport dan autentikasi layanan

Status: ready (sisi kode), waiting-external (sertifikat dan kredensial)
Sumber: release gate "TLS everywhere, Kafka SASL/ACLs, Redis AUTH, encryption at rest",
threat model S-3 / I-6 / T-6 / I-5, T-9

## Tujuan
Semua lalu lintas antar-komponen terenkripsi dan terautentikasi, dan data at-rest terenkripsi.

## Kondisi sekarang
Stack lokal plaintext. Klien Kafka (5 tempat: `rec/stream/processor.py`,
`rec/simulator/runner.py`, `scripts/loadtest.py`, `scripts/create_topics.py`) hanya menerima
`bootstrap_servers`. Redis dan Postgres sudah bisa TLS/AUTH lewat URL/DSN (`rediss://`,
`sslmode=verify-full`). API → ranking memakai `RANKING_SERVICE_URL` dan token layanan (D-5).

## Requirement
1. R1 — Kafka: TLS + SASL (SCRAM-SHA-512 atau yang ditetapkan platform), ACL per principal:
   stream baca `cc.transactions`, tulis `customer.features` dan `pipeline.dlq`; simulator
   hanya tulis `cc.transactions` dan hanya di non-produksi.
2. R2 — Satu tempat konfigurasi klien Kafka (setting `KAFKA_SECURITY_PROTOCOL`,
   `KAFKA_SASL_MECHANISM`, `KAFKA_SASL_USERNAME`, `KAFKA_SASL_PASSWORD`, `KAFKA_CA_FILE`),
   dipakai oleh semua pembuat klien.
3. R3 — Redis: AUTH (ACL user per layanan) dan TLS (`rediss://`, verifikasi CA).
4. R4 — Postgres: TLS dengan `sslmode=verify-full`.
5. R5 — API → ranking lewat HTTPS dengan verifikasi CA (atau mTLS dari service mesh).
6. R6 — Enkripsi at-rest untuk Postgres, Redis (RDB/AOF), Kafka log, dan volume model.
7. R7 — Setelah Redis AUTH aktif, status T-9 (integritas state bandit) ditinjau ulang.

## Input yang ditunggu (tim platform)
CA dan sertifikat, mekanisme SASL, kredensial per layanan, kebijakan at-rest.

## Rujukan
`docs/runbooks.md` bagian "Local stack security gaps", `docs/threat-model.md`.
