"""Threat S-2: customers sign in with the mobile channel's OIDC access token (ADR-0013)."""
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

from rec.api import auth
from rec.settings import settings

ISSUER, AUDIENCE = "https://idp.bank.example/realms/customers", "rec-engine"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idp(monkeypatch):
    key = _key()
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": "k1"}
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})
    monkeypatch.setattr(auth, "_jwks", {})
    monkeypatch.setattr(settings, "customer_jwks_url", "https://idp.bank.example/jwks")
    monkeypatch.setattr(settings, "customer_jwt_issuer", ISSUER)
    monkeypatch.setattr(settings, "customer_jwt_audience", AUDIENCE)
    monkeypatch.setattr(settings, "environment", "production")
    return key


def _token(key, *, kid="k1", alg="RS256", **claims):
    now = int(time.time())
    body = {"iss": ISSUER, "aud": AUDIENCE, "sub": "C0000001", "iat": now, "exp": now + 300}
    return jwt.encode(body | claims, key, algorithm=alg, headers={"kid": kid})


def _request(token: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [(b"authorization", f"Bearer {token}".encode())]})


async def _subject(token: str):
    try:
        p = await auth.principal(_request(token))
    except HTTPException as exc:
        return exc.status_code
    return p.kind, p.subject


async def test_only_a_valid_token_from_the_customer_idp_signs_a_customer_in(idp, monkeypatch):
    assert await _subject(_token(idp)) == ("customer", "C0000001")

    now = int(time.time())
    for forged in (
        _token(idp, aud="another-app"),
        _token(idp, iss="https://evil.example"),
        _token(idp, exp=now - 120),                      # past the 30 s leeway
        _token(idp, nbf=now + 600),
        _token(_key()),                                  # right kid, wrong key
        _token(idp, sub=""),
        jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "C0000001", "exp": now + 300},
                   None, algorithm="none"),
        "cust-C0000001",                                 # dev stand-in, dead in production
    ):
        assert await _subject(forged) == 401, forged

    no_exp = _token(idp)
    no_exp_claims = jwt.decode(no_exp, options={"verify_signature": False})
    no_exp_claims.pop("exp")
    assert await _subject(jwt.encode(no_exp_claims, idp, algorithm="RS256",
                                     headers={"kid": "k1"})) == 401

    # HS256 keyed with the public key (algorithm confusion) is refused
    pem = idp.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    signing_input = b".".join(jwt.utils.base64url_encode(json.dumps(part).encode()) for part in (
        {"alg": "HS256", "kid": "k1", "typ": "JWT"},
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "C0000001", "exp": now + 300}))
    signature = jwt.utils.base64url_encode(hmac.digest(pem, signing_input, "sha256"))
    assert await _subject((signing_input + b"." + signature).decode()) == 401

    # unconfigured: even a well-signed token is refused (fail closed)
    monkeypatch.setattr(settings, "customer_jwt_audience", "")
    assert await _subject(_token(idp)) == 401
