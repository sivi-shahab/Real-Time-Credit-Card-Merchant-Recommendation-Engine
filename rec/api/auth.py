"""SEC-001 — role enforcement happens here, on the backend (AC-006, UI gating is cosmetic).

Demo token scheme. Fase 5 replaces it with the BFF session cookie + OIDC upstream;
the Principal contract and the require() guards stay as-is.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from rec.settings import settings

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
    "audit:read": {"Auditor", "Platform Operator"},
}


@dataclass
class Principal:
    subject: str
    role: str
    kind: str  # "admin" | "customer"

    def can(self, action: str) -> bool:
        return self.kind == "admin" and self.role in PERMISSIONS.get(action, set())


def _admin_table() -> dict[str, tuple[str, str]]:
    table = {}
    for entry in settings.admin_tokens.split(","):
        if not entry.strip():
            continue
        token, subject, role = entry.split(":", 2)
        table[token.strip()] = (subject.strip(), role.strip())
    return table


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # EventSource cannot set headers, so SSE (and only SSE) accepts the token as a
    # query param. It disappears with the BFF session cookie in Fase 5.
    if request.url.path.endswith("/admin/v1/events"):
        token = request.query_params.get("access_token")
        if token:
            return token.strip()
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")


async def principal(request: Request) -> Principal:
    token = _bearer(request)
    admins = _admin_table()
    if token in admins:
        subject, role = admins[token]
        return Principal(subject, role, "admin")
    if token.startswith(settings.customer_token_prefix):
        return Principal(token[len(settings.customer_token_prefix):], "Customer", "customer")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")


def require(action: str):
    async def guard(p: Principal = Depends(principal)) -> Principal:
        if not p.can(action):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"role {p.role} may not {action}")
        return p

    return guard


async def customer_self(customer_id: str, p: Principal = Depends(principal)) -> Principal:
    """AC-006 — a customer token may only read its own resource (BOLA guard)."""
    if p.kind == "customer" and p.subject != customer_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "token does not match customer resource")
    if p.kind == "admin" and not p.can("customer:read"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role may not read customer data")
    return p
