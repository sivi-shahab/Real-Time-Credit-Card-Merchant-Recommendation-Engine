# Spec — Ketahanan data: PITR, replika, RTO/RPO

Status: waiting-external
Sumber: release gate "PITR, cross-zone replicas, restore RTO/RPO targets agreed"

## Tujuan
Target pemulihan disepakati dan terbukti lewat drill di infrastruktur produksi.

## Kondisi sekarang
`scripts/backup.sh` dan `scripts/dr_drill.sh` (backup, hancurkan, restore, rebuild Redis,
verifikasi identik) sudah dipraktikkan di stack lokal. Redis selalu bisa dibangun ulang dari
Postgres; Postgres adalah satu-satunya sumber kebenaran yang harus dilindungi.

## Requirement
1. R1 — RTO dan RPO disepakati bisnis dan platform, per komponen (Postgres, Kafka, model).
2. R2 — Postgres dengan PITR dan replika lintas zona sesuai RPO.
3. R3 — Kafka replikasi 3 dengan `min.insync.replicas` 2 (sudah di `create_topics.py`).
4. R4 — Artefak model (volume `MODEL_DIR` dan MLflow) ter-backup; digest di Postgres tetap
   memverifikasi file yang dipulihkan (T-4).
5. R5 — Drill DR dijalankan di lingkungan mirip produksi, termasuk rebuild Redis, dan waktu
   pemulihan terukur memenuhi RTO.

## Rujukan
Runbook `backup-and-restore`, `redis-loss`; `scripts/dr_drill.sh`.
