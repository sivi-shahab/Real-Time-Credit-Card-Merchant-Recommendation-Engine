"""SDD 13.4 — Backend-for-Frontend auth.

OIDC authorization code + PKCE against the corporate IdP; the BFF keeps the upstream
tokens in the server-side session and hands the browser only an opaque HttpOnly cookie.

The id_token's signature is verified against the IdP's JWKS, then its issuer, audience,
expiry and nonce (S-4). OIDC Core 3.1.3.7 would allow trusting the TLS back channel
instead; verifying costs little and does not depend on how that channel is deployed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from rec.api import auth
from rec.settings import DEV_ENVIRONMENTS, settings
from rec.store import pg

router = APIRouter(prefix="/bff", tags=["auth"])
OIDC_STATE_PREFIX = "oidc:"
_discovery: dict | None = None


def _set_cookie(response: Response, sid: str) -> None:
    response.set_cookie(settings.session_cookie, sid, httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/",
                        max_age=settings.session_absolute_seconds)


def _safe_next(target: str | None) -> str:
    """Open-redirect guard: only same-origin relative paths."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/overview"
    return target


async def _audit(request: Request, subject: str, role: str, action: str, outcome: str,
                 detail: dict | None = None) -> None:
    await pg.audit(subject, role, action, "session", outcome=outcome, changes=detail,
                   trace_id=getattr(request.state, "trace_id", None))


@router.get("/config")
async def config():
    return {"sso": bool(settings.oidc_discovery_url),
            "devLogin": settings.environment in DEV_ENVIRONMENTS}


class DevLogin(BaseModel):
    token: str


@router.post("/dev-login")
async def dev_login(body: DevLogin, request: Request, response: Response):
    """Local/test only: trade a demo token for a real session, so the browser path is
    identical to SSO from here on."""
    if settings.environment not in DEV_ENVIRONMENTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    entry = auth._admin_table().get(body.token)
    if entry is None:
        await _audit(request, "anonymous", "-", "auth.login", "FAILURE", {"method": "dev"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    subject, role = entry
    sid, csrf = await auth.create_session(subject, role)
    _set_cookie(response, sid)
    await _audit(request, subject, role, "auth.login", "SUCCESS", {"method": "dev"})
    return {"subject": subject, "role": role, "csrfToken": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get(settings.session_cookie)
    data = await auth.drop_session(sid) if sid else None
    response.delete_cookie(settings.session_cookie, path="/")
    if data:
        await _audit(request, data["subject"], data["role"], "auth.logout", "SUCCESS")
    end_session = None
    if data and data.get("upstream", {}).get("id_token") and settings.oidc_discovery_url:
        doc = await _oidc()
        if doc.get("end_session_endpoint"):
            end_session = doc["end_session_endpoint"] + "?" + urlencode({
                "id_token_hint": data["upstream"]["id_token"],
                "client_id": settings.oidc_client_id})
    return {"endSessionUrl": end_session}


# ------------------------------------------------------------------ OIDC


async def _oidc() -> dict:
    global _discovery
    if not settings.oidc_discovery_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SSO is not configured")
    if _discovery is None:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(settings.oidc_discovery_url)
            r.raise_for_status()
            _discovery = r.json()
    return _discovery


@router.get("/login")
async def login(next: str | None = None):
    doc = await _oidc()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    await auth.redis().set(OIDC_STATE_PREFIX + state, json.dumps(
        {"nonce": nonce, "verifier": verifier, "next": _safe_next(next)}), ex=600)
    return RedirectResponse(doc["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri, "scope": "openid profile",
        "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256"}), status_code=302)


def resolve_role(roles: list[str]) -> str:
    """Exactly one application role per identity. A user holding e.g. ML Engineer AND
    Approver would defeat separation of duties, so that is refused, not merged."""
    app_roles = [r for r in roles if r in auth.ROLES]
    if len(app_roles) != 1:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"identity must hold exactly one application role, has {app_roles}")
    return app_roles[0]


@router.get("/callback")
async def callback(request: Request, code: str, state: str):
    raw = await auth.redis().getdel(OIDC_STATE_PREFIX + state)  # single use
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown or expired login state")
    pending = json.loads(raw)
    doc = await _oidc()
    async with httpx.AsyncClient(timeout=5) as c:
        tok = await c.post(doc["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": settings.oidc_redirect_uri,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
            "code_verifier": pending["verifier"]})
        if tok.status_code != 200:
            await _audit(request, "anonymous", "-", "auth.login", "FAILURE",
                         {"method": "oidc", "status": tok.status_code})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "code exchange failed")
        tokens = tok.json()
        try:
            claims = await auth.verified_claims(
                tokens["id_token"], doc["jwks_uri"], issuer=doc["issuer"],
                audience=settings.oidc_client_id, require=["sub", "nonce"])
        except jwt.PyJWTError:
            claims = None
        if claims is None or claims["nonce"] != pending["nonce"]:
            await _audit(request, "anonymous", "-", "auth.login", "FAILURE",
                         {"method": "oidc", "reason": "id_token rejected"})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id_token rejected")
        info = await c.get(doc["userinfo_endpoint"],
                           headers={"authorization": f"Bearer {tokens['access_token']}"})
        info.raise_for_status()
    userinfo = info.json()
    subject = userinfo.get("preferred_username") or claims["sub"]
    try:
        role = resolve_role(list(userinfo.get(settings.oidc_roles_claim) or []))
    except HTTPException as exc:
        await _audit(request, subject, "-", "auth.login", "DENIED", {"reason": exc.detail})
        raise
    sid, _ = await auth.create_session(subject, role, upstream={
        "id_token": tokens["id_token"], "refresh_token": tokens.get("refresh_token")})
    response = RedirectResponse(pending["next"], status_code=302)
    _set_cookie(response, sid)
    await _audit(request, subject, role, "auth.login", "SUCCESS", {"method": "oidc"})
    return response
