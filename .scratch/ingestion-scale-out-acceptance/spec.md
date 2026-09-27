# Spec — Scale-out ingestion dan uji penerimaan (ADR-0014)

Status: waiting-external
Sumber: ADR-0014, release gate "Ingestion 10 000 TPS steady / 20 000 burst"

## Tujuan
Ingestion mencapai 10.000 transaksi/detik stabil dan 20.000/detik saat burst, terbukti di
hardware yang mirip produksi.

## Kondisi sekarang
Sisi kode selesai: ledger keluar dari pembacaan per event, key per customer ber-hash-tag
dan siap Redis Cluster, feature update digabung per batch, `create_topics.py`,
`PG_STATEMENT_CACHE_SIZE`, `loadtest.py kafka`. Terukur lokal: 8 consumer 1.070 event/detik;
Redis tidak lagi jadi batas.

## Requirement
1. R1 — Topic produksi dibuat dengan `scripts/create_topics.py` (96 partisi, replikasi 3,
   `min.insync.replicas` 2).
2. R2 — Consumer di-deploy dengan autoscale berdasarkan consumer lag, minimum ~50, maksimum 96.
3. R3 — Redis Cluster (mulai 6 primary + replika) dengan `REDIS_CLUSTER=true`.
4. R4 — PgBouncer mode transaction; `PG_STATEMENT_CACHE_SIZE=0` bila tanpa prepared statements.
5. R5 — Rebuild Redis satu kali saat deploy pertama, dengan durasi yang diperkirakan dan
   dijadwalkan.
6. R6 — Uji penerimaan: 10.000/detik 30 menit dengan lag terbatas; 20.000/detik 5 menit,
   lag pulih ≤ 10 menit.

## Input yang ditunggu (tim platform)
Hardware atau kuota cloud, Kafka minimal 3 broker, Redis Cluster, PgBouncer, pipeline deploy.

## Di luar cakupan
Menulis ulang consumer di luar Python (ditolak di ADR-0014, ditinjau ulang setelah uji).

## Rujukan
`docs/adr/0014-ingestion-scale-out.md`, runbook `kafka-topics`, `pgbouncer`,
`stream-scaling`, `redis-loss`; ringkasan untuk tim platform (README → Write-ups).
