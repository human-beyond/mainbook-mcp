"""HTTP boundary tests for lazy OAuth and stable refusal responses."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx2 as httpx
import pytest

from mainbook_mcp.oauth_http import (
    INTERNAL_OAUTH_HEADER,
    TOOL_SCOPES,
    OAuthToolAuthMiddleware,
    current_oauth_request_state,
    decode_internal_identity,
)
from mainbook_mcp.oauth_verifier import OAuthVerificationError, VerifiedOAuthToken

SUBJECT = "11111111-1111-4111-8111-111111111111"
METADATA = "https://mcp.mainbook.ai/.well-known/oauth-protected-resource/mcp"


@dataclass
class StubVerifier:
    scopes: frozenset[str] = frozenset({"mainbook:read", "mainbook:convert"})
    failure: str | None = None

    async def verify(self, token: str) -> VerifiedOAuthToken:
        if self.failure is not None:
            raise OAuthVerificationError(self.failure)
        import uuid

        return VerifiedOAuthToken(
            subject=uuid.UUID(SUBJECT),
            client_id="client-1",
            consent_id="consent-1",
            jti="access-jti",
            scopes=self.scopes,
        )


def tool_request(name: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


def app_with(verifier: StubVerifier):
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:
        calls.append(scope)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{}'})

    return OAuthToolAuthMiddleware(downstream, verifier), calls


@pytest.mark.asyncio
async def test_initialize_and_tools_list_stay_anonymous_for_catalog_health() -> None:
    """Glama and repository review bots must inspect health and tools without a credential."""
    app, calls = app_with(StubVerifier(failure="must_not_be_called"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        initialize = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        listed = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )

    assert initialize.status_code == listed.status_code == 200
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic x"},
        {"Authorization": "Bearer"},
    ],
)
async def test_missing_and_malformed_credentials_have_one_opaque_401(headers) -> None:
    app, calls = app_with(StubVerifier())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        response = await client.post("/mcp", json=tool_request("get_balance"), headers=headers)

    assert response.status_code == 401
    assert response.content == b'{"error":"invalid_token"}'
    assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{METADATA}"'
    assert calls == []


@pytest.mark.asyncio
async def test_all_verifier_failures_share_the_same_public_401() -> None:
    observed: list[tuple[bytes, str]] = []
    for reason in (
        "token_structure",
        "expired",
        "issuer",
        "audience",
        "unknown_kid",
        "inactive_user",
        "revoked",
    ):
        app, calls = app_with(StubVerifier(failure=reason))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
        ) as client:
            response = await client.post(
                "/mcp",
                json=tool_request("get_balance"),
                headers={"Authorization": "Bearer opaque-client-token"},
            )
        observed.append((response.content, response.headers["www-authenticate"]))
        assert calls == []

    assert len(set(observed)) == 1


@pytest.mark.asyncio
async def test_scope_denial_is_403_before_conversion_handler() -> None:
    app, calls = app_with(StubVerifier(scopes=frozenset({"mainbook:read"})))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        response = await client.post(
            "/mcp",
            json=tool_request("convert_bank_statement"),
            headers={"Authorization": "Bearer signed-token"},
        )

    assert response.status_code == 403
    assert response.content == b'{"error":"insufficient_scope"}'
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]
    assert 'scope="mainbook:convert"' in response.headers["www-authenticate"]
    assert calls == []


@pytest.mark.asyncio
async def test_legacy_key_path_is_unchanged_and_spoofed_internal_marker_is_removed() -> None:
    app, calls = app_with(StubVerifier(failure="must_not_be_called"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        response = await client.post(
            "/mcp",
            json=tool_request("get_balance"),
            headers={
                "Authorization": "Bearer mb_live_existing_key",
                INTERNAL_OAUTH_HEADER.decode(): "attacker-marker",
            },
        )

    assert response.status_code == 200
    assert len(calls) == 1
    assert INTERNAL_OAUTH_HEADER not in {name for name, _ in calls[0]["headers"]}


@pytest.mark.asyncio
async def test_verified_identity_marker_is_internal_and_contains_no_client_token() -> None:
    app, calls = app_with(StubVerifier())
    client_token = "signed-client-token-never-forward"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        response = await client.post(
            "/mcp",
            json=tool_request("get_balance"),
            headers={"Authorization": f"Bearer {client_token}"},
        )

    assert response.status_code == 200
    internal = [value for name, value in calls[0]["headers"] if name == INTERNAL_OAUTH_HEADER]
    assert len(internal) == 1
    identity = decode_internal_identity(internal[0].decode("ascii"))
    assert identity == (SUBJECT, "client-1", "consent-1")
    assert client_token.encode() not in internal[0]


@pytest.mark.asyncio
async def test_downstream_inactive_or_revoked_state_becomes_the_same_opaque_401() -> None:
    async def downstream(scope, receive, send) -> None:
        del scope, receive
        state = current_oauth_request_state.get()
        assert state is not None
        state.mark_downstream_auth_failed()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"tool":"error detail"}'})

    app = OAuthToolAuthMiddleware(downstream, StubVerifier())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        response = await client.post(
            "/mcp",
            json=tool_request("get_balance"),
            headers={"Authorization": "Bearer signed-token"},
        )

    assert response.status_code == 401
    assert response.content == b'{"error":"invalid_token"}'
    assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{METADATA}"'


@pytest.mark.asyncio
async def test_progress_reaches_client_before_handler_finishes() -> None:
    """The middleware must not buffer progress or the eventual XLSX response."""
    handler_may_finish = asyncio.Event()
    progress_reached_client = asyncio.Event()

    async def downstream(scope, receive, send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b'{"progress":25}\n',
                "more_body": True,
            }
        )
        await handler_may_finish.wait()
        await send({"type": "http.response.body", "body": b'{"done":true}'})

    app = OAuthToolAuthMiddleware(downstream, StubVerifier())
    request_sent = False

    async def receive():
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {
            "type": "http.request",
            "body": b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"convert_bank_statement","arguments":{}}}',
            "more_body": False,
        }

    messages = []

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and b"progress" in message.get(
            "body", b""
        ):
            progress_reached_client.set()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer signed-token")],
    }
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(progress_reached_client.wait(), timeout=0.25)
    assert not task.done()
    handler_may_finish.set()
    await task

    assert [message["type"] for message in messages] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]


def test_every_hosted_tool_has_one_declared_scope() -> None:
    assert TOOL_SCOPES == {
        "convert_bank_statement": "mainbook:convert",
        "get_balance": "mainbook:read",
        "get_conversion": "mainbook:read",
        "list_conversions": "mainbook:read",
    }


@pytest.mark.asyncio
async def test_security_logs_contain_reason_but_no_token_secret_or_code(caplog) -> None:
    app, _calls = app_with(StubVerifier(failure="expired"))
    credential_material = "token-secret-code-material"
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
        ) as client:
            await client.post(
                "/mcp",
                json=tool_request("get_balance"),
                headers={"Authorization": f"Bearer {credential_material}"},
            )

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert credential_material not in rendered
    assert "secret" not in rendered
    assert "code" not in rendered
    assert any(getattr(record, "reason", None) == "expired" for record in caplog.records)
