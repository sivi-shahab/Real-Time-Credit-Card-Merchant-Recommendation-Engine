# ADR-0006 — Browser auth through a BFF session, SSO through OIDC

**Status:** Accepted · 2026-09-25

## Context
Fase 1–4 shipped bearer tokens held in `sessionStorage`, with SSE passing the token as a
query parameter. SDD 13.4 requires a BFF holding upstream tokens, an `HttpOnly` session
cookie, CSRF protection on mutations, and no access token in browser storage. SEC-001
requires SSO and separation of duties.

## Decision
1. **Opaque server-side session.** `/bff/*` (in the API process) issues a random 256-bit
   session id in an `HttpOnly; SameSite=Lax; Secure` cookie. The session (subject, role,
   CSRF token, upstream OIDC tokens) lives in Redis with a 30-minute sliding idle timeout
   and an 8-hour absolute lifetime. Logout deletes it server-side, so a copied cookie dies.
2. **CSRF: synchroniser token.** `/admin/v1/me` returns the session's CSRF token; every
   non-GET request authenticated by cookie must echo it in `X-CSRF-Token`. The dashboard
   keeps it in memory only. Bearer-authenticated requests are not CSRF-exposed and skip it.
3. **OIDC authorization code + PKCE (S256)** against any compliant IdP; locally Keycloak
   with one demo user per role. State is single-use and expires in 10 minutes; nonce,
   issuer, audience and expiry of the id_token are checked, and since 2026-09-27 its
   signature too, against the IdP's `jwks_uri` (RS256/ES256; S-4). OIDC Core 3.1.3.7 would
   allow trusting the TLS back channel instead; with PyJWT already in for customer tokens
   (ADR-0013) verifying costs little and holds however that channel is deployed.
4. **One identity, one application role.** An IdP user holding two application roles
   (e.g. ML Engineer + Approver) is refused at login rather than merged — merging would
   silently defeat maker-checker on promotion and erasure.
5. **Bearer stays for machines and the customer channel.** Static admin tokens are
   honoured only when `ENVIRONMENT` is local/test/ci. Customer `cust-<id>` tokens remain
   the stand-in for the mobile channel's own auth; the BOLA guard (AC-006) is unchanged.

## Consequences
- The dashboard must be same-origin with the API (nginx / Vite proxy `/api`, `/admin`,
  `/bff`). SSE now works with the cookie; the query-token exception is gone.
- Redis loss logs every admin out. Accepted: sessions are cheap to re-create via SSO.
- Machine-to-machine admin access in production needs a real credential (OIDC client
  credentials) — not built; scripts use static tokens in local/staging only.
