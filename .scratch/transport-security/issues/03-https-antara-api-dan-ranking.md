# 03 — HTTPS antara API dan ranking

Status: ready
Type: task
Owner: tim aplikasi

## Yang dikerjakan
- Setting `RANKING_CA_FILE` (verifikasi CA) untuk klien httpx di `rec/ml/client.py`.
- Atau dokumentasikan bahwa mTLS disediakan service mesh, dan tolak `http://` di non-dev.

## Kriteria selesai
Keputusan tercatat; bila di aplikasi, test klien dengan CA lokal lulus.
