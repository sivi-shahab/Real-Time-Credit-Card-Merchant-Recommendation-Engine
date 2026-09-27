"""SDD 16 security level: BFF session + CSRF, OIDC, RBAC matrix, IDOR, injection, leakage.

Needs postgres + redis from docker-compose (sessions live in Redis).
"""
from __future__ import annotations

import logging
import re
import time

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from rec.api import auth, bff
from rec.api.app import app
from rec.obs import RedactingFilter
from rec.settings import settings

TOKENS = {"Platform Operator": "admin-token", "Analyst": "analyst-token",
          "Marketing Operator": "ops-token", "Auditor": "auditor-token",
          "ML Engineer": "ml-token", "Approver": "approver-token", "Viewer": "viewer-token"}


@pytest_asyncio.fixture(loop_scope="session")
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _login(client, token="admin-token") -> str:
    r = await client.post("/bff/dev-login", json={"token": token})
    assert r.status_code == 200, r.text
    return r.json()["csrfToken"]


# ----------------------------------------------------------------- BFF session


async def test_session_cookie_is_httponly_and_carries_no_token(client):
    r = await client.post("/bff/dev-login", json={"token": "admin-token"})
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "admin-token" not in cookie  # opaque id, not the credential
    me = (await client.get("/admin/v1/me")).json()
    assert me["role"] == "Platform Operator" and me["csrfToken"] == r.json()["csrfToken"]


async def test_mutation_without_csrf_token_is_refused(client):
    csrf = await _login(client, "ops-token")
    body = {"status": "ACTIVE", "version": 1}
    r = await client.patch("/admin/v1/merchants/NOPE", json=body)
    assert r.status_code == 403 and "CSRF" in r.json()["message"]
    r = await client.patch("/admin/v1/merchants/NOPE", json=body,
                           headers={"x-csrf-token": "forged"})
    assert r.status_code == 403
    r = await client.patch("/admin/v1/merchants/NOPE", json=body,
                           headers={"x-csrf-token": csrf})
    assert r.status_code != 403  # past auth; 404/409 is the handler's business


async def test_logout_kills_the_session_server_side(client):
    await _login(client)
    sid = client.cookies.get(settings.session_cookie)
    assert (await client.post("/bff/logout")).status_code == 200
    client.cookies.set(settings.session_cookie, sid)  # replay the stolen cookie
    assert (await client.get("/admin/v1/me")).status_code == 401


async def test_sse_no_longer_accepts_a_token_in_the_url(client):
    r = await client.get("/admin/v1/events?access_token=admin-token")
    assert r.status_code == 401


async def test_static_tokens_are_dead_outside_dev(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    r = await client.get("/admin/v1/me", headers={"Authorization": "Bearer admin-token"})
    assert r.status_code == 401
    assert (await client.post("/bff/dev-login", json={"token": "admin-token"})).status_code == 404
    # the forgeable cust-<id> stand-in fails closed too (threat model S-2)
    r = await client.get("/api/v1/customer/C0000001/recommendations",
                         headers={"Authorization": "Bearer cust-C0000001"})
    assert r.status_code == 401


def test_model_version_cannot_escape_the_model_directory(tmp_path, monkeypatch):
    """Threat model T-3: the version string becomes a file name."""
    from rec.ranking import service as ranking

    (tmp_path / "models").mkdir()
    (tmp_path / "planted.json").write_text("{}")
    monkeypatch.setattr(settings, "model_dir", str(tmp_path / "models"))
    for version in ("../planted", "..", "a/b", "/etc/passwd"):
        with pytest.raises(FileNotFoundError, match="invalid model version"):
            ranking.registry.get(version, "0" * 64)


def test_open_redirect_is_blocked():
    assert bff._safe_next("//evil.example/x") == "/overview"
    assert bff._safe_next("https://evil.example") == "/overview"
    assert bff._safe_next("/\\evil.example") == "/overview"
    assert bff._safe_next("/models?x=1") == "/models?x=1"


def test_one_identity_one_role():
    assert bff.resolve_role(["offline_access", "Approver"]) == "Approver"
    for roles in (["ML Engineer", "Approver"], ["offline_access"]):
        with pytest.raises(HTTPException) as exc:
            bff.resolve_role(roles)
        assert exc.value.status_code == 403


# ----------------------------------------------------------------- OIDC


_IDP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwt(claims: dict, key=_IDP_KEY) -> str:
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "idp-1"})


@pytest.fixture
def idp(monkeypatch):
    """A fake IdP behind httpx.MockTransport; `state` lets a test tamper with it."""
    issuer = "http://idp.test/realms/rec"
    state = {"nonce": None, "roles": ["ML Engineer"], "aud": settings.oidc_client_id,
             "verifier_seen": None, "key": _IDP_KEY}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/token"):
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            state["verifier_seen"] = form.get("code_verifier")
            return httpx.Response(200, json={"access_token": "at", "id_token": _jwt({
                "iss": issuer, "aud": state["aud"], "sub": "u1", "nonce": state["nonce"],
                "exp": time.time() + 60}, state["key"])})
        if req.url.path.endswith("/userinfo"):
            return httpx.Response(200, json={"preferred_username": "dina",
                                             "roles": state["roles"]})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(bff.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handle)))
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_IDP_KEY.public_key(), as_dict=True)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data",
                        lambda self: {"keys": [jwk | {"kid": "idp-1"}]})
    monkeypatch.setattr(auth, "_jwks", {})
    monkeypatch.setattr(settings, "oidc_discovery_url", issuer + "/.well-known")
    monkeypatch.setattr(bff, "_discovery", {
        "issuer": issuer, "authorization_endpoint": issuer + "/auth",
        "token_endpoint": issuer + "/token", "userinfo_endpoint": issuer + "/userinfo",
        "jwks_uri": issuer + "/certs"})
    return state


async def _start(client, idp) -> str:
    r = await client.get("/bff/login", params={"next": "/models"})
    assert r.status_code == 302
    q = dict(httpx.URL(r.headers["location"]).params)
    assert q["code_challenge_method"] == "S256"
    idp["nonce"] = q["nonce"]
    return q["state"]


async def test_oidc_code_flow_creates_a_session(client, idp):
    state = await _start(client, idp)
    r = await client.get("/bff/callback", params={"code": "c", "state": state})
    assert r.status_code == 302 and r.headers["location"] == "/models"
    assert idp["verifier_seen"]  # PKCE verifier went over the back channel
    me = (await client.get("/admin/v1/me")).json()
    assert (me["subject"], me["role"]) == ("dina", "ML Engineer")
    # state is single use
    again = await client.get("/bff/callback", params={"code": "c", "state": state})
    assert again.status_code == 400


async def test_oidc_rejects_wrong_nonce_audience_signature_or_role_set(client, idp):
    state = await _start(client, idp)
    idp["nonce"] = "replayed"
    assert (await client.get("/bff/callback",
                             params={"code": "c", "state": state})).status_code == 401
    state = await _start(client, idp)
    idp["aud"] = "some-other-client"
    assert (await client.get("/bff/callback",
                             params={"code": "c", "state": state})).status_code == 401
    state = await _start(client, idp)
    idp["aud"] = settings.oidc_client_id
    idp["key"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert (await client.get("/bff/callback", params={"code": "c", "state": state})
            ).status_code == 401, "an id_token not signed by the IdP (S-4)"
    state = await _start(client, idp)
    idp["key"], idp["roles"] = _IDP_KEY, ["ML Engineer", "Approver"]
    assert (await client.get("/bff/callback",
                             params={"code": "c", "state": state})).status_code == 403


# ----------------------------------------------------------------- RBAC matrix


def _admin_routes():
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/admin/"):
            guards = [d.call for d in route.dependant.dependencies
                      if hasattr(d.call, "action")]
            yield route, guards[0].action if guards else None


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_admin_route_is_guarded():
    unguarded = {r.path for r, action in _admin_routes() if action is None}
    # /me reports on whoever is signed in; /customers/{id}/features uses customer_self
    assert unguarded <= {"/admin/v1/me", "/admin/v1/customers/{customer_id}/features"}, \
        unguarded


async def test_rbac_matrix_every_role_every_route(client):
    """Every role that lacks the permission gets 403; nobody unauthenticated gets in."""
    checked = 0
    for route, action in _admin_routes():
        method = sorted(route.methods)[0]
        url = _url(route.path)
        r = await client.request(method, url)
        assert r.status_code == 401, (method, url, r.status_code)
        if action is None:
            continue
        for role, token in TOKENS.items():
            if role in auth.PERMISSIONS[action]:
                continue
            r = await client.request(method, url, headers={"Authorization": f"Bearer {token}"},
                                     json={})
            assert r.status_code == 403, (role, method, url, r.status_code)
            checked += 1
    assert checked > 80


async def test_denials_are_audited(client):
    await client.post("/admin/v1/training-jobs", json={},
                      headers={"Authorization": "Bearer viewer-token"})
    rows = (await client.get("/admin/v1/audit-events",
                             headers={"Authorization": "Bearer auditor-token"})).json()["items"]
    assert any(r["action"] == "authz.denied" and r["actor"] == "viewer"
               and r["outcome"] == "DENIED" for r in rows)


# ----------------------------------------------------------------- injection / leakage


async def test_sql_metacharacters_are_data_not_code(client):
    r = await client.get("/admin/v1/merchants", params={"search": "' OR 1=1 --"},
                         headers={"Authorization": "Bearer admin-token"})
    assert r.status_code == 200
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    assert items == []


def test_logs_redact_credentials():
    record = logging.LogRecord("x", logging.INFO, __file__, 1,
                               "auth header Bearer abc.def-123 cookie rec_session=s3cr3t "
                               "dsn postgresql://rec:hunter2@db/rec pan 4111111111111111",
                               None, None)
    RedactingFilter().filter(record)
    msg = record.getMessage()
    for secret in ("abc.def-123", "s3cr3t", "hunter2", "4111111111111111"):
        assert secret not in msg, msg


async def test_errors_are_uniform_and_requests_are_bounded(client):
    """Threat model I-4 / D-2: errors carry code+message+traceId and nothing internal;
    oversized requests are refused by the schema before any work is done."""
    admin = {"Authorization": "Bearer admin-token"}
    r = await client.get("/admin/v1/datasets/no-such-dataset", headers=admin)
    body = r.json()
    assert r.status_code == 404 and set(body) == {"code", "message", "fieldErrors", "traceId"}
    assert body["traceId"] == r.headers["x-correlation-id"]
    assert "Traceback" not in r.text and "postgresql://" not in r.text
    cust = {"Authorization": "Bearer cust-C0000001"}
    r = await client.get("/api/v1/customer/C0000001/recommendations?limit=21", headers=cust)
    assert r.status_code == 422
    r = await client.get("/admin/v1/audit-events?limit=201",
                         headers={"Authorization": "Bearer auditor-token"})
    assert r.status_code == 422
