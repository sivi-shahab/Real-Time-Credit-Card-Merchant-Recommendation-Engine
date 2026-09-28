# 03 — Superset di docker-compose dengan SSO Keycloak

Status: resolved
Type: task
Owner: tim aplikasi
Blocked by: 02

## Yang dikerjakan
- Service `superset` (image resmi, versi dikunci) dan database metadata terpisah.
- Koneksi ke Postgres sebagai `rec_analytics`.
- OIDC ke realm `rec` di Keycloak, pemetaan peran (lihat R5), client baru di
  `deploy/keycloak/rec-realm.json`.

## Kriteria selesai
Login `analyst` lewat SSO berhasil dan hanya bisa membaca; `operator` menjadi admin Superset;
README dan tabel alamat layanan diperbarui.

## Comments

2026-09-29 — Login SSO dicek dengan alur browser sungguhan. `analyst` hanya membaca
(Gamma + Analytics Reader; buat chart, SQL Lab dan CSV 403); hak tulis Gamma untuk
chart, dashboard dan tag dicabut di `provision.py` setelah `superset init`. `operator`
menjadi Admin dan satu-satunya yang bisa export CSV. `mlengineer` tetap bisa membuat chart
dan memakai SQL Lab. README (tabel alamat, login) dan threat model (B7, I-8, I-10) diperbarui.
