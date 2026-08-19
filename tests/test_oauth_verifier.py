"""Adversarial tests for the local MainBook OAuth JWT verifier."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mainbook_mcp.oauth_verifier import (
    DEFAULT_ISSUER,
    DEFAULT_RESOURCE,
    JWKSCache,
    OAuthSettings,
    OAuthTokenVerifier,
    OAuthVerificationError,
)

NOW = 1_787_140_800
SUBJECT = "11111111-1111-4111-8111-111111111111"


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(key, kid: str) -> dict[str, object]:
    value = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**value, "kid": kid, "use": "sig", "alg": "RS256"}


def claims(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "iss": DEFAULT_ISSUER,
        "sub": SUBJECT,
        "aud": DEFAULT_RESOURCE,
        "iat": NOW,
        "exp": NOW + 600,
        "jti": "22222222-2222-4222-8222-222222222222",
        "client_id": "sample-public-client",
        "scope": "mainbook:read mainbook:convert",
        "consent_id": "33333333-3333-4333-8333-333333333333",
    }
    value.update(updates)
    return value


def access_token(key, *, kid: str = "current", payload=None, headers=None) -> str:
    return jwt.encode(
        claims() if payload is None else payload,
        key,
        algorithm="RS256",
        headers={"kid": kid, **(headers or {})},
    )


def verifier_for(keys: list[tuple[str, object]], *, clock=None, requests=None):
    jwks = {"keys": [public_jwk(key, kid) for kid, key in keys]}

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(str(request.url))
        return httpx.Response(200, json=jwks)

    settings = OAuthSettings(
        enabled=True,
        service_signing_secret="test-service-secret",
        jwks_refresh_min_interval_seconds=1,
    )
    cache = JWKSCache(
        url=settings.jwks_url,
        ttl_seconds=300,
        refresh_min_interval_seconds=1,
        transport=httpx.MockTransport(handler),
        monotonic=(clock or (lambda: 0.0)),
    )
    return OAuthTokenVerifier(settings, jwks_cache=cache, wall_clock=lambda: NOW)


@pytest.mark.asyncio
async def test_valid_current_and_previous_rotation_keys_are_accepted() -> None:
    current = rsa_key()
    previous = rsa_key()
    verifier = verifier_for([("current", current), ("previous", previous)])

    first = await verifier.verify(access_token(current))
    overlap = await verifier.verify(access_token(previous, kid="previous"))

    assert str(first.subject) == SUBJECT
    assert overlap.client_id == "sample-public-client"
    assert overlap.scopes == {"mainbook:read", "mainbook:convert"}


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["none", "HS256", "HS384"])
async def test_algorithm_substitution_is_rejected_before_key_use(algorithm: str) -> None:
    key = rsa_key()
    verifier = verifier_for([("current", key)])
    if algorithm == "none":
        token = jwt.encode(claims(), "", algorithm="none", headers={"kid": "current"})
    else:
        public_der = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        token = jwt.encode(claims(), public_der, algorithm=algorithm, headers={"kid": "current"})

    with pytest.raises(OAuthVerificationError):
        await verifier.verify(token)


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_once_and_never_uses_token_supplied_url() -> None:
    key = rsa_key()
    requests: list[str] = []
    monotonic_now = [0.0]
    verifier = verifier_for(
        [("current", key)],
        clock=lambda: monotonic_now[0],
        requests=requests,
    )
    await verifier.verify(access_token(key))
    monotonic_now[0] = 2.0
    unknown = access_token(
        key,
        kid="not-published",
        headers={"jku": "https://attacker.example/jwks.json"},
    )

    with pytest.raises(OAuthVerificationError, match="unknown_kid"):
        await verifier.verify(unknown)

    assert requests == [
        "https://api.mainbook.ai/.well-known/jwks.json",
        "https://api.mainbook.ai/.well-known/jwks.json",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"iss": "https://attacker.example"}, "issuer"),
        ({"aud": "https://attacker.example/mcp"}, "audience"),
        ({"aud": f"{DEFAULT_RESOURCE}/"}, "audience"),
        ({"aud": [DEFAULT_RESOURCE]}, "audience"),
        ({"exp": NOW - 6}, "expired"),
        ({"iat": NOW + 6}, "issued_in_future"),
        ({"iat": NOW - 606, "exp": NOW + 1}, "token_too_old"),
        ({"exp": NOW + 606}, "token_lifetime"),
        ({"nbf": NOW + 6}, "not_yet_valid"),
        ({"sub": "bookkeeper@example.com"}, "subject"),
    ],
)
async def test_exact_identity_audience_and_time_contract(updates, reason: str) -> None:
    key = rsa_key()
    verifier = verifier_for([("current", key)])

    with pytest.raises(OAuthVerificationError, match=reason):
        await verifier.verify(access_token(key, payload=claims(**updates)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    ["exp", "iat", "sub", "jti", "client_id", "consent_id", "scope"],
)
async def test_required_claims_cannot_be_omitted(missing: str) -> None:
    key = rsa_key()
    verifier = verifier_for([("current", key)])
    payload = claims()
    del payload[missing]

    with pytest.raises(OAuthVerificationError):
        await verifier.verify(access_token(key, payload=payload))


@pytest.mark.asyncio
async def test_bad_signature_and_oversized_token_fail_closed() -> None:
    trusted = rsa_key()
    attacker = rsa_key()
    verifier = verifier_for([("current", trusted)])

    with pytest.raises(OAuthVerificationError, match="signature"):
        await verifier.verify(access_token(attacker))
    with pytest.raises(OAuthVerificationError, match="token_size"):
        await verifier.verify("x" * 8193)


def test_enabled_settings_are_explicit_and_fail_closed() -> None:
    with pytest.raises(ValueError, match="MCP_SERVICE_SIGNING_SECRETS"):
        OAuthSettings.from_env({"MAINBOOK_MCP_OAUTH_ENABLED": "true"})

    configured = OAuthSettings.from_env(
        {
            "MAINBOOK_MCP_OAUTH_ENABLED": "true",
            "MCP_SERVICE_SIGNING_SECRETS": "new-secret,old-secret",
        }
    )
    assert configured.service_signing_secret == "new-secret"
    assert replace(configured, enabled=False).enabled is False


def test_disabled_flag_ignores_dormant_invalid_oauth_settings() -> None:
    settings = OAuthSettings.from_env(
        {
            "MAINBOOK_MCP_OAUTH_ENABLED": "false",
            "MAINBOOK_MCP_OAUTH_JWKS_CACHE_TTL_SECONDS": "not-an-integer",
            "MAINBOOK_MCP_OAUTH_ISSUER": "not-a-url",
        }
    )
    assert settings == OAuthSettings()


def test_jwks_fixture_shape_matches_backend_public_contract() -> None:
    key = rsa_key()
    jwk = public_jwk(key, "oauth-current-example")
    assert json.loads(json.dumps({"keys": [jwk]}))["keys"][0] == jwk
    assert {"kty", "kid", "use", "alg", "n", "e"} <= set(jwk)
