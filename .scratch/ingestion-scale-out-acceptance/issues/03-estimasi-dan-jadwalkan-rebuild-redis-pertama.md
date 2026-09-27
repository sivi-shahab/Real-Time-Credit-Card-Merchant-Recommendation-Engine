# 03 — Estimasi dan jadwalkan rebuild Redis pertama

Status: blocked
Type: task
Owner: tim platform + tim aplikasi
Blocked by: 01

## Konteks
`scripts/rebuild_state.py --flush` memutar ulang seluruh `transaction_log` lewat satu proses
(lokal ~230 event/detik). Dengan volume produksi bisa berjam-jam; sesi staf ikut hilang.

## Yang dikerjakan
- Ukur laju rebuild di lingkungan uji dengan Redis Cluster; hitung durasi dari jumlah baris log.
- Kalau durasinya tidak bisa diterima: tambahkan opsi rebuild paralel per rentang customer
  (tiket terpisah), karena urutan hanya perlu terjaga per customer.
- Jadwalkan jendela deploy dan umumkan ke pengguna dashboard.

## Kriteria selesai
Durasi terukur, jendela deploy disetujui, langkahnya tercatat di runbook `redis-loss`.
