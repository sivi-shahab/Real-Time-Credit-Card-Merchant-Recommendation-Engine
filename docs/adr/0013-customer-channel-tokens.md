# ADR-0013 — Customers authenticate with the mobile channel's OIDC access token

**Status:** Accepted, pending confirmation of the token profile by the mobile platform ·
2026-09-27

## Context
The customer API (`/api/v1/customer/{id}/...`) accepted `Bearer cust-<id>`, a guessable
stand-in that is refused outside local/test/ci (threat S-2). Nothing customer-facing could
go live. The mobile platform's token format has not been specified to us; the common
profile for a bank's app is an OIDC access token, a signed JWT from the customer IdP.

## Decision
1. **Verify a JWT against the customer IdP's JWKS**, in the API, on every request:
   signature (RS256 or ES256 only, so the public key can never act as an HMAC secret),
   `iss`, `aud`, `exp` and `nbf` with 30 s leeway. `exp`, `iss`, `aud` and the customer
   claim must be present.
2. **The customerId comes from one configurable claim** (`CUSTOMER_ID_CLAIM`, default
   `sub`); `customer_self` then limits the token to that customer's resources (BOLA).
3. **Configuration or nothing**: `CUSTOMER_JWKS_URL`, `CUSTOMER_JWT_ISSUER` and
   `CUSTOMER_JWT_AUDIENCE` must all be set, else no customer token is accepted.
4. Keys are cached for an hour; a token with an unknown `kid` refetches them, so IdP key
   rotation needs no restart. PyJWT does the cryptography; none is written here.

## Consequences
- The customer API can go live once the IdP values are configured. Until then it fails
  closed in production, as before.
- The mobile platform must confirm: the issuer, an audience dedicated to this API, the
  algorithm, and which claim carries our customerId. If it is not `sub`, the IdP adds a
  claim mapper. If the channel uses mTLS or opaque tokens (introspection) instead, this
  ADR is superseded.
- Revocation takes effect only at `exp`: keep access tokens short-lived (≤ 15 min).
- A JWKS outage refuses customers whose key is not yet cached (401); cached keys keep
  working through it.
