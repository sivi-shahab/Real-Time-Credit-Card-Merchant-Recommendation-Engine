# Spec — Integrasi IdP customer (S-2)

Status: waiting-external
Sumber: ADR-0013, threat model S-2, release gate "Customer channel authentication"

## Tujuan
API customer menerima access token OIDC dari aplikasi mobile banking di produksi, sehingga
rekomendasi dan feedback bisa dipakai nasabah sungguhan.

## Kondisi sekarang
Verifikasi JWT sudah dibangun dan teruji (`rec/api/auth.py`, `tests/test_customer_auth.py`):
tanda tangan RS256/ES256 lewat JWKS, `iss`, `aud`, `exp`, `nbf` (leeway 30 detik), customerId
dari satu claim. Tanpa konfigurasi, semua token customer ditolak di luar local/test/ci.

## Requirement
1. R1 — Token diverifikasi terhadap IdP customer bank: `CUSTOMER_JWKS_URL`,
   `CUSTOMER_JWT_ISSUER`, `CUSTOMER_JWT_AUDIENCE` terisi di setiap lingkungan non-dev.
2. R2 — customerId diambil dari claim yang disepakati (`CUSTOMER_ID_CLAIM`), dengan format
   yang sama dengan `customers.customer_id`.
3. R3 — Access token berumur paling lama 15 menit (pencabutan baru berlaku saat `exp`).
4. R4 — Alur end-to-end diuji dengan token asli dari IdP staging: rekomendasi, impressions,
   interactions; penolakan untuk token kedaluwarsa, audience salah, issuer salah, dan token
   milik customer lain.
5. R5 — Lonjakan 401 di API customer terlihat di monitoring dan ber-alert.

## Input yang ditunggu (tim mobile)
- Jenis token (JWT OIDC; kalau mTLS atau token opaque, ADR-0013 diganti).
- Issuer, URL JWKS, audience khusus untuk API ini, algoritma.
- Claim yang berisi customerId, atau claim mapper di IdP.
- IdP staging dan beberapa user uji.

## Di luar cakupan
Registrasi aplikasi di IdP, alur login di aplikasi, refresh token (urusan aplikasi dan IdP).

## Rujukan
`docs/adr/0013-customer-channel-tokens.md`, ringkasan untuk tim mobile (README → Write-ups),
`contracts/openapi.json`.
