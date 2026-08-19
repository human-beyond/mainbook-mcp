"""Lazy HTTP authentication for MCP tool calls only."""

from __future__ import annotations

import base64
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .oauth_verifier import (
    PROTECTED_RESOURCE_METADATA_URL,
    OAuthTokenVerifier,
    OAuthVerificationError,
    VerifiedOAuthToken,
)

logger = logging.getLogger(__name__)

TOOL_SCOPES: dict[str, str] = {
    "convert_bank_statement": "mainbook:convert",
    "get_balance": "mainbook:read",
    "get_conversion": "mainbook:read",
    "list_conversions": "mainbook:read",
}

INVALID_TOKEN_BODY = b'{"error":"invalid_token"}'
INSUFFICIENT_SCOPE_BODY = b'{"error":"insufficient_scope"}'
INTERNAL_OAUTH_HEADER = b"x-mainbook-internal-oauth"


@dataclass
class OAuthRequestState:
    """Mutable state shared with child tasks created for one verified tool call."""

    downstream_auth_failed: bool = False

    def mark_downstream_auth_failed(self) -> None:
        self.downstream_auth_failed = True


current_oauth_request_state: ContextVar[OAuthRequestState | None] = ContextVar(
    "mainbook_mcp_oauth_request_state", default=None
)


class OAuthToolAuthMiddleware:
    """Authenticate ``tools/call`` while leaving discovery and tool listing public."""

    def __init__(self, app: ASGIApp, verifier: OAuthTokenVerifier) -> None:
        self._app = app
        self._verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        body, replay = await _buffer_body(receive)
        tool_name = _tool_call_name(body)
        if tool_name is None:
            await self._app(scope, replay, send)
            return
        clean_scope = _without_internal_header(scope)

        token = _bearer_token(clean_scope)
        if token is None:
            logger.info(
                "mcp_oauth_auth_failed",
                extra={"reason": "missing_or_malformed_authorization", "tool": tool_name},
            )
            await _send_error(send, status=401, body=INVALID_TOKEN_BODY)
            return

        # Legacy keys keep their byte-for-byte downstream path. Django remains
        # their source of truth for revocation and account state.
        if token.startswith("mb_live_"):
            await self._app(clean_scope, replay, send)
            return

        try:
            verified = await self._verifier.verify(token)
        except OAuthVerificationError as exc:
            logger.info(
                "mcp_oauth_auth_failed",
                extra={"reason": exc.reason, "tool": tool_name},
            )
            await _send_error(send, status=401, body=INVALID_TOKEN_BODY)
            return

        required_scope = TOOL_SCOPES.get(tool_name)
        if required_scope is not None and required_scope not in verified.scopes:
            logger.info(
                "mcp_oauth_scope_denied",
                extra={"reason": "insufficient_scope", "tool": tool_name},
            )
            await _send_error(
                send,
                status=403,
                body=INSUFFICIENT_SCOPE_BODY,
                required_scope=required_scope,
            )
            return

        state = OAuthRequestState()
        pending_start: Message | None = None
        response_committed = False

        async def guarded_send(message: Message) -> None:
            """Delay only headers, then preserve downstream streaming verbatim."""
            nonlocal pending_start, response_committed
            if message["type"] == "http.response.start":
                pending_start = message
                return
            if message["type"] == "http.response.body" and not response_committed:
                if state.downstream_auth_failed:
                    return
                if pending_start is None:
                    raise RuntimeError("Response body sent before response start")
                await send(pending_start)
                pending_start = None
                response_committed = True
            await send(message)

        marker = current_oauth_request_state.set(state)
        try:
            await self._app(_with_internal_identity(clean_scope, verified), replay, guarded_send)
        finally:
            current_oauth_request_state.reset(marker)
        if state.downstream_auth_failed and not response_committed:
            await _send_error(send, status=401, body=INVALID_TOKEN_BODY)
            return
        if pending_start is not None and not response_committed:
            await send(pending_start)


async def _buffer_body(receive: Receive) -> tuple[bytes, Receive]:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay


def _tool_call_name(body: bytes) -> str | None:
    try:
        payload: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return None
    params = payload.get("params")
    if not isinstance(params, dict):
        return ""
    name = params.get("name")
    return name if isinstance(name, str) else ""


def _bearer_token(scope: Scope) -> str | None:
    values = [
        value.decode("latin-1")
        for name, value in scope.get("headers", [])
        if name.decode("latin-1").casefold() == "authorization"
    ]
    if len(values) != 1:
        return None
    scheme, separator, token = values[0].partition(" ")
    clean = token.strip()
    if scheme.casefold() != "bearer" or not separator or not clean:
        return None
    return clean


def _without_internal_header(scope: Scope) -> Scope:
    clean = dict(scope)
    clean["headers"] = [
        (name, value)
        for name, value in scope.get("headers", [])
        if name.lower() != INTERNAL_OAUTH_HEADER
    ]
    return clean


def _with_internal_identity(scope: Scope, token: VerifiedOAuthToken) -> Scope:
    payload = json.dumps(
        {
            "sub": str(token.subject),
            "client_id": token.client_id,
            "consent_id": token.consent_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    marked = dict(scope)
    marked["headers"] = [*scope.get("headers", []), (INTERNAL_OAUTH_HEADER, encoded)]
    return marked


def decode_internal_identity(value: str) -> tuple[str, str, str] | None:
    """Decode only the marker injected after successful verification."""
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    subject = payload.get("sub")
    client_id = payload.get("client_id")
    consent_id = payload.get("consent_id")
    if not all(isinstance(item, str) for item in (subject, client_id, consent_id)):
        return None
    return subject, client_id, consent_id


async def _send_error(
    send: Send,
    *,
    status: int,
    body: bytes,
    required_scope: str | None = None,
) -> None:
    challenge = f'Bearer resource_metadata="{PROTECTED_RESOURCE_METADATA_URL}"'
    if required_scope is not None:
        challenge = (
            'Bearer error="insufficient_scope", '
            f'scope="{required_scope}", '
            f'resource_metadata="{PROTECTED_RESOURCE_METADATA_URL}"'
        )
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"www-authenticate", challenge.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
