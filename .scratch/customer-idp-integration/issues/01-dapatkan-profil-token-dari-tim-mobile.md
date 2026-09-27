# 01 — Dapatkan profil token dari tim mobile

Status: waiting-external
Type: task
Owner: tim mobile

## Konteks
ADR-0013 mengasumsikan access token berupa JWT OIDC. Asumsi ini harus dikonfirmasi.

## Yang dikerjakan
Minta dan catat di tiket ini: jenis token, issuer, URL JWKS, audience, algoritma, claim
customerId, masa berlaku token, serta akses ke IdP staging dan user uji.

## Kriteria selesai
- Semua nilai di atas tercatat di `## Comments`.
- Kalau jenis token bukan JWT (mTLS atau opaque), buka ADR baru yang menggantikan ADR-0013
  sebelum lanjut ke tiket 02.
