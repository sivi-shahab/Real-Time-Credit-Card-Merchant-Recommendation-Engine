# 02 — Buat topic dan deploy consumer dengan autoscale lag

Status: blocked
Type: task
Owner: tim platform
Blocked by: 01

## Yang dikerjakan
- `python scripts/create_topics.py --bootstrap <brokers> --partitions 96 --replication-factor 3`.
- Deployment `python -m rec.stream.processor`, `STREAM_GROUP_ID` sama, autoscale berdasarkan
  `kafka_consumergroup_lag` (min 50, max 96).
- `REDIS_CLUSTER=true`, `PG_STATEMENT_CACHE_SIZE` sesuai versi PgBouncer.

## Kriteria selesai
`create_topics.py --dry-run` keluar dengan kode 0 (tidak ada perbedaan); consumer group
stabil dengan jumlah pod sesuai beban.
