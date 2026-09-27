# 03 — Superset di docker-compose dengan SSO Keycloak

Status: blocked
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
