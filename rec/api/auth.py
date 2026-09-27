"""SEC-001 — role enforcement happens here, on the backend (AC-006, UI gating is cosmetic).

Two ways in:
- Browser: the BFF session cookie (SDD 13.4). Opaque id, state in Redis, HttpOnly, and
  every mutation must echo the session's CSRF token in `X-CSRF-Token`.
- Machines/customers: `Authorization: Bearer`. Static admin tokens are honoured only in
  DEV_ENVIRONMENTS. Customers present the mobile channel's OIDC access token, verified
  against the customer IdP (S-2, ADR-0013); `cust-<id>` is a dev-only stand-in.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status

from rec.settings import DEV_ENVIRONMENTS, settings
from rec.store import pg
from rec.store.redis_store import _client

SESSION_PREFIX = "sess:"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_redis = None


def redis():
    global _redis
    if _redis is None:
        _redis = _client()
    return _redis

ROLES = ["Viewer", "Analyst", "Marketing Operator", "ML Engineer", "Platform Operator",
         "Approver", "Auditor"]

# least privilege: action -> roles allowed
PERMISSIONS: dict[str, set[str]] = {
    "dataset:create": {"ML Engineer", "Platform Operator"},
    "dataset:read": {"Viewer", "Analyst", "ML Engineer", "Platform Operator", "Auditor"},
    "simulation:control": {"Platform Operator", "ML Engineer"},
    "transaction:read": {"Analyst", "Platform Operator", "Auditor"},
    "transaction:replay": {"Platform Operator"},
    "customer:read": {"Analyst", "Platform Operator"},
    "recommendation:preview": {"Analyst", "ML Engineer", "Marketing Operator"},
    "merchant:read": {"Viewer", "Analyst", "Marketing Operator", "Platform Operator", "Auditor"},
    "merchant:write": {"Marketing Operator"},
    "promotion:read": {"Viewer", "Analyst", "Marketing Operator", "Platform Operator", "Auditor"},
    "promotion:write": {"Marketing Operator"},
    "promotion:approve": {"Approver"},
    "metrics:read": {"Viewer", "Analyst", "Marketing Operator", "ML Engineer",
                     "Platform Operator", "Auditor"},
    "training:run": {"ML Engineer"},
    "model:read": {"Viewer", "Analyst", "ML Engineer", "Platform Operator", "Approver",
                   "Auditor"},
    # SEC-001: separation of duties — the engineer who trains a model does not promote it.
    "model:promote": {"Approver"},
    "model:rollback": {"Approver", "Platform Operator"},
    "audit:read": {"Auditor", "Platform Operator"},
    # AC-009 maker-checker: an operator files, an approver (a different person) executes.
    "erasure:request": {"Platform Operator"},
    "erasure:approve": {"Approver"},
    "erasure:read": {"Platform Operator", "Approver", "Auditor"},
    # ADR-0011 maker-checker on the learning switches: one files, an Approver decides.
    "learning:request": {"ML Engineer", "Platform Operator"},
    "learning:approve": {"Approver"},
}


@dataclass
class Principal:
    subject: str
    role: str
    kind: str  # "admin" | "customer"
    session_id: str | None = None
    csrf: str | None = None

    def can(self, action: str) -> bool:
        return self.kind == "admin" and self.role in PERMISSIONS.get(action, set())


def _admin_table() -> dict[str, tuple[str, str]]:
    if settings.environment not in DEV_ENVIRONMENTS:
        return {}
    table = {}
    for entry in settings.admin_tokens.split(","):
        if not entry.strip():
            continue
        token, subject, role = entry.split(":", 2)
        table[token.strip()] = (subject.strip(), role.strip())
    return table


# ------------------------------------------------------------------ sessions


async def create_session(subject: str, role: str, upstream: dict | None = None) -> tuple[str, str]:
    """Returns (session_id, csrf). Upstream OIDC tokens stay here, never in the browser."""
    sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    data = {"subject": subject, "role": role, "csrf": csrf, "createdAt": time.time(),
            "upstream": upstream or {}}
    await redis().set(SESSION_PREFIX + sid, json.dumps(data), ex=settings.session_idle_seconds)
    return sid, csrf


async def load_session(sid: str) -> dict | None:
    key = SESSION_PREFIX + sid
    raw = await redis().get(key)
    if not raw:
        return None
    data = json.loads(raw)
    if time.time() - data["createdAt"] > settings.session_absolute_seconds:
        await redis().delete(key)
        return None
    await redis().expire(key, settings.session_idle_seconds)  # sliding idle timeout
    return data


async def drop_session(sid: str) -> dict | None:
    key = SESSION_PREFIX + sid
    raw = await redis().getdel(key)
    return json.loads(raw) if raw else None


async def _from_session(request: Request, sid: str) -> Principal:
    data = await load_session(sid)
    if data is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, data["csrf"]):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or invalid CSRF token")
    return Principal(data["subject"], data["role"], "admin", sid, data["csrf"])


# Asymmetric only: an HMAC algorithm would let the IdP's public key sign tokens.
CUSTOMER_JWT_ALGORITHMS = ["RS256", "ES256"]
_jwks: jwt.PyJWKClient | None = None


async def _customer_from_jwt(token: str) -> str | None:
    """The customerId a valid customer IdP token names, else None (S-2).

    Signature against the IdP's published keys, then issuer, audience, expiry and
    not-before, with 30 s of clock leeway. Unconfigured means no customer gets in.
    """
    global _jwks
    if not (settings.customer_jwks_url and settings.customer_jwt_issuer
            and settings.customer_jwt_audience):
        return None
    if _jwks is None:  # keys are cached; an unknown kid refetches them (key rotation)
        _jwks = jwt.PyJWKClient(settings.customer_jwks_url, cache_keys=True, lifespan=3600)
    claim = settings.customer_id_claim
    try:
        # PyJWKClient fetches over blocking urllib when its cache misses
        key = await asyncio.to_thread(_jwks.get_signing_key_from_jwt, token)
        claims = jwt.decode(token, key.key, algorithms=CUSTOMER_JWT_ALGORITHMS,
                            issuer=settings.customer_jwt_issuer,
                            audience=settings.customer_jwt_audience, leeway=30,
                            options={"require": ["exp", "iss", "aud", claim]})
    except jwt.PyJWTError:
        return None
    subject = claims.get(claim)
    return subject if isinstance(subject, str) and subject else None


async def principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        sid = request.cookies.get(settings.session_cookie)
        if sid:
            return await _from_session(request, sid)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    token = header[7:].strip()
    admins = _admin_table()
    if token in admins:
        subject, role = admins[token]
        return Principal(subject, role, "admin")
    # The cust-<id> scheme is a forgeable stand-in for the mobile channel's token and
    # works only in dev environments.
    if (token.startswith(settings.customer_token_prefix)
            and settings.environment in DEV_ENVIRONMENTS):
        return Principal(token[len(settings.customer_token_prefix):], "Customer", "customer")
    customer_id = await _customer_from_jwt(token)
    if customer_id is not None:
        return Principal(customer_id, "Customer", "customer")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")


def require(action: str):
    async def guard(request: Request, p: Principal = Depends(principal)) -> Principal:
        if not p.can(action):
            if p.kind == "admin":  # an admin probing beyond their role is worth a record
                await pg.audit(p.subject, p.role, "authz.denied", request.url.path,
                               outcome="DENIED", changes={"action": action},
                               trace_id=getattr(request.state, "trace_id", None))
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"role {p.role} may not {action}")
        return p

    guard.action = action  # read by the RBAC matrix test
    return guard


async def customer_self(customer_id: str, p: Principal = Depends(principal)) -> Principal:
    """AC-006 — a customer token may only read its own resource (BOLA guard)."""
    if p.kind == "customer" and p.subject != customer_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "token does not match customer resource")
    if p.kind == "admin" and not p.can("customer:read"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role may not read customer data")
    return p
