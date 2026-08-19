"""Fail-closed verifier for MainBook OAuth access tokens."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2 as httpx
import jwt

DEFAULT_ISSUER = "https://api.mainbook.ai"
DEFAULT_JWKS_URL = "https://api.mainbook.ai/.well-known/jwks.json"
DEFAULT_RESOURCE = "https://mcp.mainbook.ai/mcp"
PROTECTED_RESOURCE_METADATA_URL = (
    "https://mcp.mainbook.ai/.well-known/oauth-protected-resource/mcp"
)
SUPPORTED_SCOPES = ("mainbook:convert", "mainbook:read")
MAX_TOKEN_BYTES = 8192
MAX_JWKS_BYTES = 256 * 1024
MAX_JWKS_KEYS = 32
MAX_CLAIM_LENGTH = 255


class OAuthConfigurationError(ValueError):
    """OAuth is enabled but its trusted local configuration is incomplete."""


class OAuthVerificationError(Exception):
    """Internal-only token rejection whose reason contains no credential material."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _env_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise OAuthConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise OAuthConfigurationError(f"{name} must be positive")
    return value


def _nonnegative_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise OAuthConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise OAuthConfigurationError(f"{name} must not be negative")
    return value


def _https_url(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise OAuthConfigurationError(f"{name} must be an absolute HTTPS URL")
    if parsed.query or parsed.fragment:
        raise OAuthConfigurationError(f"{name} must not contain query or fragment data")
    return value


@dataclass(frozen=True)
class OAuthSettings:
    """Trusted hosted-mode settings; no value is derived from an access token."""

    enabled: bool = False
    issuer: str = DEFAULT_ISSUER
    jwks_url: str = DEFAULT_JWKS_URL
    resource: str = DEFAULT_RESOURCE
    service_signing_secret: str = ""
    clock_skew_seconds: int = 5
    max_token_age_seconds: int = 600
    jwks_cache_ttl_seconds: int = 300
    jwks_refresh_min_interval_seconds: int = 30

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> OAuthSettings:
        current = os.environ if environ is None else environ
        enabled = _env_bool(current.get("MAINBOOK_MCP_OAUTH_ENABLED"))
        if not enabled:
            # The dark-launch flag must preserve the old hosted startup path even
            # if dormant OAuth variables are incomplete or malformed.
            return cls()
        settings = cls(
            enabled=enabled,
            issuer=current.get("MAINBOOK_MCP_OAUTH_ISSUER", DEFAULT_ISSUER).strip(),
            jwks_url=current.get("MAINBOOK_MCP_OAUTH_JWKS_URL", DEFAULT_JWKS_URL).strip(),
            resource=current.get("MAINBOOK_MCP_OAUTH_RESOURCE", DEFAULT_RESOURCE).strip(),
            service_signing_secret=_first_secret(
                current.get("MCP_SERVICE_SIGNING_SECRETS", "")
            ),
            clock_skew_seconds=_nonnegative_int(
                current, "MAINBOOK_MCP_OAUTH_CLOCK_SKEW_SECONDS", 5
            ),
            max_token_age_seconds=_positive_int(
                current, "MAINBOOK_MCP_OAUTH_MAX_TOKEN_AGE_SECONDS", 600
            ),
            jwks_cache_ttl_seconds=_positive_int(
                current, "MAINBOOK_MCP_OAUTH_JWKS_CACHE_TTL_SECONDS", 300
            ),
            jwks_refresh_min_interval_seconds=_positive_int(
                current, "MAINBOOK_MCP_OAUTH_JWKS_REFRESH_MIN_INTERVAL_SECONDS", 30
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.enabled:
            return
        _https_url(self.issuer, "MAINBOOK_MCP_OAUTH_ISSUER")
        _https_url(self.jwks_url, "MAINBOOK_MCP_OAUTH_JWKS_URL")
        _https_url(self.resource, "MAINBOOK_MCP_OAUTH_RESOURCE")
        if not self.service_signing_secret:
            raise OAuthConfigurationError(
                "MCP_SERVICE_SIGNING_SECRETS must contain a signing secret when OAuth is enabled"
            )
        for name, value in (
            ("clock_skew_seconds", self.clock_skew_seconds),
            ("max_token_age_seconds", self.max_token_age_seconds),
            ("jwks_cache_ttl_seconds", self.jwks_cache_ttl_seconds),
            ("jwks_refresh_min_interval_seconds", self.jwks_refresh_min_interval_seconds),
        ):
            if value < 0 or (name != "clock_skew_seconds" and value == 0):
                raise OAuthConfigurationError(f"{name} has an invalid value")


def _first_secret(raw: str) -> str:
    return next((part.strip() for part in raw.split(",") if part.strip()), "")


@dataclass(frozen=True)
class VerifiedOAuthToken:
    subject: uuid.UUID
    client_id: str
    consent_id: str
    jti: str
    scopes: frozenset[str]


class JWKSCache:
    """Bounded JWKS cache with one rate-limited refresh for an unknown ``kid``."""

    def __init__(
        self,
        *,
        url: str,
        ttl_seconds: int,
        refresh_min_interval_seconds: int,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._ttl_seconds = ttl_seconds
        self._refresh_min_interval_seconds = refresh_min_interval_seconds
        self._monotonic = monotonic
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
        )
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._last_refresh_attempt: float | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def key_for(self, kid: str) -> Any:
        fetched_now = False
        now = self._monotonic()
        if self._fetched_at is None or now - self._fetched_at >= self._ttl_seconds:
            await self._refresh(force=True)
            fetched_now = True
        if key := self._keys.get(kid):
            return key
        if not fetched_now:
            await self._refresh(force=False)
        key = self._keys.get(kid)
        if key is None:
            raise OAuthVerificationError("unknown_kid")
        return key

    async def _refresh(self, *, force: bool) -> None:
        async with self._lock:
            now = self._monotonic()
            if (
                not force
                and self._last_refresh_attempt is not None
                and now - self._last_refresh_attempt < self._refresh_min_interval_seconds
            ):
                return
            if (
                force
                and self._fetched_at is not None
                and now - self._fetched_at < self._ttl_seconds
            ):
                return
            self._last_refresh_attempt = now
            try:
                response = await self._http.get(self._url, headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                raise OAuthVerificationError("jwks_unavailable") from exc
            if response.status_code != 200 or len(response.content) > MAX_JWKS_BYTES:
                raise OAuthVerificationError("jwks_unavailable")
            try:
                payload = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OAuthVerificationError("jwks_invalid") from exc
            self._keys = _parse_jwks(payload)
            self._fetched_at = self._monotonic()


def _parse_jwks(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise OAuthVerificationError("jwks_invalid")
    raw_keys = payload["keys"]
    if not raw_keys or len(raw_keys) > MAX_JWKS_KEYS:
        raise OAuthVerificationError("jwks_invalid")
    parsed: dict[str, Any] = {}
    for raw in raw_keys:
        if not isinstance(raw, dict):
            continue
        kid = raw.get("kid")
        if (
            not isinstance(kid, str)
            or not kid
            or len(kid) > MAX_CLAIM_LENGTH
            or kid in parsed
            or raw.get("kty") != "RSA"
            or raw.get("alg") not in (None, "RS256")
            or raw.get("use") not in (None, "sig")
        ):
            continue
        try:
            parsed[kid] = jwt.PyJWK.from_dict(raw, algorithm="RS256").key
        except (jwt.PyJWKError, ValueError, TypeError):
            continue
    if not parsed:
        raise OAuthVerificationError("jwks_invalid")
    return parsed


class OAuthTokenVerifier:
    """Verify signature and the exact public claim contract in fail-closed order."""

    def __init__(
        self,
        settings: OAuthSettings,
        *,
        jwks_cache: JWKSCache | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        settings.validate()
        self.settings = settings
        self._wall_clock = wall_clock
        self._jwks = jwks_cache or JWKSCache(
            url=settings.jwks_url,
            ttl_seconds=settings.jwks_cache_ttl_seconds,
            refresh_min_interval_seconds=settings.jwks_refresh_min_interval_seconds,
        )

    async def verify(self, token: str) -> VerifiedOAuthToken:
        if not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise OAuthVerificationError("token_size")
        parts = token.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            raise OAuthVerificationError("token_structure")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise OAuthVerificationError("token_header") from exc
        if header.get("alg") != "RS256":
            raise OAuthVerificationError("algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > MAX_CLAIM_LENGTH:
            raise OAuthVerificationError("kid")
        key = await self._jwks.key_for(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                options={
                    "verify_aud": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_iss": False,
                    "verify_nbf": False,
                    "require": [],
                },
            )
        except jwt.InvalidTokenError as exc:
            raise OAuthVerificationError("signature") from exc
        return self._validate_claims(claims)

    def _validate_claims(self, claims: object) -> VerifiedOAuthToken:
        if not isinstance(claims, dict):
            raise OAuthVerificationError("claims")
        if claims.get("iss") != self.settings.issuer:
            raise OAuthVerificationError("issuer")
        if claims.get("aud") != self.settings.resource:
            raise OAuthVerificationError("audience")

        now = self._wall_clock()
        exp = _numeric_date(claims, "exp")
        iat = _numeric_date(claims, "iat")
        raw_nbf = claims.get("nbf")
        nbf = _numeric_date(claims, "nbf") if raw_nbf is not None else None
        skew = self.settings.clock_skew_seconds
        if exp <= now - skew:
            raise OAuthVerificationError("expired")
        if iat > now + skew:
            raise OAuthVerificationError("issued_in_future")
        if now - iat > self.settings.max_token_age_seconds + skew:
            raise OAuthVerificationError("token_too_old")
        if exp <= iat:
            raise OAuthVerificationError("token_lifetime")
        if exp - iat > self.settings.max_token_age_seconds + skew:
            raise OAuthVerificationError("token_lifetime")
        if nbf is not None and nbf > now + skew:
            raise OAuthVerificationError("not_yet_valid")

        raw_sub = _required_string(claims, "sub")
        try:
            subject = uuid.UUID(raw_sub)
        except (ValueError, AttributeError):
            raise OAuthVerificationError("subject") from None
        if str(subject) != raw_sub.lower():
            raise OAuthVerificationError("subject")

        jti = _required_string(claims, "jti")
        client_id = _required_string(claims, "client_id")
        consent_id = _required_string(claims, "consent_id")
        raw_scope = claims.get("scope")
        if not isinstance(raw_scope, str):
            raise OAuthVerificationError("scope")
        scopes = frozenset(raw_scope.split())
        return VerifiedOAuthToken(
            subject=subject,
            client_id=client_id,
            consent_id=consent_id,
            jti=jti,
            scopes=scopes,
        )


def _numeric_date(claims: Mapping[str, object], name: str) -> int:
    value = claims.get(name)
    if type(value) is not int:
        raise OAuthVerificationError(f"{name}_claim")
    return value


def _required_string(claims: Mapping[str, object], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value or len(value) > MAX_CLAIM_LENGTH:
        raise OAuthVerificationError(f"{name}_claim")
    return value
