# 01 — Satu konfigurasi klien Kafka dengan TLS dan SASL

Status: ready
Type: task
Owner: tim aplikasi

## Yang dikerjakan
- Setting baru di `rec/settings.py` (lihat R2 di spec), default plaintext agar lokal tetap jalan.
- Satu fungsi `kafka_client_options()` yang dipakai kelima pembuat klien Kafka.
- Test: opsi yang dihasilkan untuk plaintext, SASL_SSL + SCRAM, dan CA file; satu uji manual
  terhadap broker lokal yang dikonfigurasi SASL_SSL (compose override khusus uji).

## Kriteria selesai
Semua klien memakai fungsi yang sama; test lulus; runbook `secrets-and-rotation` menjelaskan
setting dan rotasi kredensial Kafka.
