"""Transport tests derived from D8 task spec §§4, 6 and 7."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import jwt
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from mainbook_mcp import __main__
from mainbook_mcp import server as server_module
from mainbook_mcp.client import MainBookClient, ServiceCredentialIssuer
from mainbook_mcp.oauth_verifier import OAuthSettings, VerifiedOAuthToken
from mainbook_mcp.server import create_server

ENV_KEY = "mb_live_environment_fallback"
FIRST_KEY = "mb_live_http_first_request"
SECOND_KEY = "mb_live_http_second_request"


class BalanceClient:
    async def get_balance(self) -> dict[str, int]:
        return {"balance": 10, "reserved": 1, "available": 9}


class NeverVerifier:
    async def verify(self, token: str):
        raise AssertionError("initialize and tools/list must not invoke OAuth verification")


class AcceptingVerifier:
    async def verify(self, token: str) -> VerifiedOAuthToken:
        assert token == "raw-client-oauth-token"
        return VerifiedOAuthToken(
            subject=uuid.UUID("11111111-1111-4111-8111-111111111111"),
            client_id="sample-public-client",
            consent_id="33333333-3333-4333-8333-333333333333",
            jti="22222222-2222-4222-8222-222222222222",
            scopes=frozenset({"mainbook:read"}),
        )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for_port(port: int) -> None:
    for _ in range(100):
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.02)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise AssertionError("HTTP MCP server did not start")


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth_enabled", [False, True])
async def test_http_header_key_overrides_environment_and_never_leaks_between_requests(
    monkeypatch,
    oauth_enabled: bool,
) -> None:
    """HTTP uses only each request header and must never inspect local credential storage."""
    monkeypatch.setenv("MAINBOOK_API_KEY", ENV_KEY)
    monkeypatch.setattr(
        server_module,
        "load_credential",
        lambda api_base: (_ for _ in ()).throw(AssertionError("local storage was read")),
    )
    captured_keys: list[str] = []

    @asynccontextmanager
    async def factory(api_key: str, base_url: str) -> AsyncIterator[BalanceClient]:
        captured_keys.append(api_key)
        yield BalanceClient()

    oauth_settings = OAuthSettings(
        enabled=oauth_enabled,
        service_signing_secret="service-secret-for-test" if oauth_enabled else "",
    )
    server = create_server(
        transport="http",
        client_factory=factory,
        oauth_settings=oauth_settings,
        oauth_verifier=NeverVerifier() if oauth_enabled else None,  # type: ignore[arg-type]
    )
    port = free_port()
    task = asyncio.create_task(
        server.run_streamable_http_async(
            host="127.0.0.1",
            port=port,
            json_response=True,
            stateless_http=True,
        )
    )
    await wait_for_port(port)

    try:
        for key in (FIRST_KEY, SECOND_KEY):
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http_client,
                Client(
                    streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http_client)
                ) as client,
            ):
                result = await client.call_tool("get_balance", {})
                assert result.structured_content is not None
                assert result.structured_content["available"] == 9
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert captured_keys == [FIRST_KEY, SECOND_KEY]
    assert ENV_KEY not in captured_keys


@pytest.mark.asyncio
async def test_resource_metadata_exists_only_when_oauth_flag_is_enabled() -> None:
    enabled = OAuthSettings(enabled=True, service_signing_secret="service-secret-for-test")
    enabled_app = create_server(transport="http", oauth_settings=enabled).streamable_http_app(
        stateless_http=True, json_response=True
    )
    disabled_app = create_server(
        transport="http", oauth_settings=OAuthSettings()
    ).streamable_http_app(stateless_http=True, json_response=True)

    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=enabled_app),
            base_url="https://mcp.mainbook.ai",
        ) as enabled_client,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=disabled_app),
            base_url="https://mcp.mainbook.ai",
        ) as disabled_client,
    ):
        published = await enabled_client.get("/.well-known/oauth-protected-resource/mcp")
        absent = await disabled_client.get("/.well-known/oauth-protected-resource/mcp")

    assert published.status_code == 200
    assert published.json() == {
        "resource": "https://mcp.mainbook.ai/mcp",
        "authorization_servers": ["https://api.mainbook.ai"],
        "scopes_supported": ["mainbook:convert", "mainbook:read"],
        "bearer_methods_supported": ["header"],
    }
    assert "api.mainbook.ai/.well-known/oauth-protected-resource" not in published.text
    assert absent.status_code == 404


@pytest.mark.asyncio
async def test_real_http_initialize_and_tools_list_are_anonymous_with_oauth_enabled() -> None:
    """Catalog health depends on both handshake and tool discovery staying anonymous."""
    settings = OAuthSettings(enabled=True, service_signing_secret="service-secret-for-test")
    server = create_server(
        transport="http",
        oauth_settings=settings,
        oauth_verifier=NeverVerifier(),  # type: ignore[arg-type]
    )
    port = free_port()
    task = asyncio.create_task(
        server.run_streamable_http_async(
            host="127.0.0.1",
            port=port,
            json_response=True,
            stateless_http=True,
        )
    )
    await wait_for_port(port)

    try:
        async with Client(streamable_http_client(f"http://127.0.0.1:{port}/mcp")) as client:
            listed = await client.list_tools()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert {tool.name for tool in listed.tools} == {
        "convert_bank_statement",
        "get_balance",
        "get_conversion",
        "list_conversions",
    }


@pytest.mark.asyncio
async def test_real_oauth_tool_call_sends_only_service_credential_to_developer_api() -> None:
    secret = "service-door-secret-for-transport-test"
    raw_client_token = "raw-client-oauth-token"
    captured: list[httpx2.Request] = []

    def django_stub(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, json={"balance": 10, "reserved": 1, "available": 9})

    @asynccontextmanager
    async def factory(credential, base_url: str):
        assert isinstance(credential, ServiceCredentialIssuer)
        async with MainBookClient(
            service_credential=credential,
            base_url=base_url,
            transport=httpx2.MockTransport(django_stub),
        ) as client:
            yield client

    settings = OAuthSettings(enabled=True, service_signing_secret=secret)
    server = create_server(
        transport="http",
        oauth_settings=settings,
        oauth_verifier=AcceptingVerifier(),  # type: ignore[arg-type]
        client_factory=factory,
    )
    port = free_port()
    task = asyncio.create_task(
        server.run_streamable_http_async(
            host="127.0.0.1",
            port=port,
            json_response=True,
            stateless_http=True,
        )
    )
    await wait_for_port(port)

    try:
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {raw_client_token}"}
            ) as http_client,
            Client(
                streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http_client)
            ) as client,
        ):
            result = await client.call_tool("get_balance", {})
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert result.is_error is not True
    assert len(captured) == 1
    sent = captured[0]
    assert "authorization" not in sent.headers
    assert raw_client_token not in str(dict(sent.headers))
    service_claims = jwt.decode(
        sent.headers["x-mainbook-service"],
        secret,
        algorithms=["HS256"],
        audience="api.mainbook.ai",
        issuer="mcp",
    )
    assert service_claims["sub"] == "11111111-1111-4111-8111-111111111111"
    assert service_claims["cid"] == "sample-public-client"
    assert service_claims["src"] == "oauth"
    assert service_claims["gid"] == "33333333-3333-4333-8333-333333333333"
    assert service_claims["exp"] - service_claims["iat"] == 60


@pytest.mark.asyncio
async def test_real_service_door_401_is_the_same_outer_oauth_401() -> None:
    """Inactive-user now, and revoked-consent in 4.6, use the opaque boundary response."""
    secret = "service-door-secret-long-enough-for-transport-test"

    @asynccontextmanager
    async def factory(credential, base_url: str):
        assert isinstance(credential, ServiceCredentialIssuer)
        async with MainBookClient(
            service_credential=credential,
            base_url=base_url,
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(401, json={"detail": "invalid_key"})
            ),
        ) as client:
            yield client

    server = create_server(
        transport="http",
        oauth_settings=OAuthSettings(enabled=True, service_signing_secret=secret),
        oauth_verifier=AcceptingVerifier(),  # type: ignore[arg-type]
        client_factory=factory,
    )
    port = free_port()
    task = asyncio.create_task(
        server.run_streamable_http_async(
            host="127.0.0.1",
            port=port,
            json_response=True,
            stateless_http=True,
        )
    )
    await wait_for_port(port)
    observed: list[tuple[int, str | None]] = []

    async def record_response(response: httpx2.Response) -> None:
        observed.append((response.status_code, response.headers.get("www-authenticate")))

    try:
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer raw-client-oauth-token"},
                event_hooks={"response": [record_response]},
            ) as http_client,
            Client(
                streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http_client)
            ) as client,
        ):
            with pytest.raises(MCPError):
                await client.call_tool("get_balance", {})
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert observed[-1] == (
        401,
        'Bearer resource_metadata="https://mcp.mainbook.ai/.well-known/'
        'oauth-protected-resource/mcp"',
    )


def test_cli_defaults_to_stdio(monkeypatch) -> None:
    """D8 task spec §4: mainbook-mcp without flags is the local stdio server."""
    calls: list[tuple[str, dict[str, object]]] = []
    modes: list[str] = []

    class Server:
        def run(self, transport, **kwargs) -> None:
            calls.append((transport, kwargs))

    def create_server(*, transport: str, allowed_roots, api_base: str) -> Server:
        assert api_base == "https://api.mainbook.ai"
        modes.append(transport)
        return Server()

    monkeypatch.setattr(__main__, "create_server", create_server)

    __main__.main([])

    assert calls == [("stdio", {})]
    assert modes == ["stdio"]


def test_cli_http_mode_is_stateless_json(monkeypatch) -> None:
    """D8 task spec §4: --transport http selects stateless Streamable HTTP JSON."""
    calls: list[tuple[str, dict[str, object]]] = []
    modes: list[str] = []

    class Server:
        def run(self, transport, **kwargs) -> None:
            calls.append((transport, kwargs))

    def create_server(*, transport: str, allowed_roots, api_base: str) -> Server:
        assert api_base == "https://api.mainbook.ai"
        modes.append(transport)
        return Server()

    monkeypatch.setattr(__main__, "create_server", create_server)

    __main__.main(
        ["--transport", "http", "--host", "0.0.0.0", "--port", "8123"]  # noqa: S104
    )

    assert calls == [
        (
            "streamable-http",
            {
                "host": "0.0.0.0",  # noqa: S104 - asserted CLI value, test does not bind it
                "port": 8123,
                "stateless_http": True,
                "json_response": True,
            },
        )
    ]
    assert modes == ["http"]


def test_cli_transport_can_be_selected_by_environment(monkeypatch) -> None:
    """D8 task spec §4: one entry point supports flag or environment selection."""
    calls: list[tuple[str, dict[str, object]]] = []
    modes: list[str] = []
    monkeypatch.setenv("MAINBOOK_MCP_TRANSPORT", "http")
    monkeypatch.setenv("MAINBOOK_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MAINBOOK_MCP_PORT", "9001")

    class Server:
        def run(self, transport, **kwargs) -> None:
            calls.append((transport, kwargs))

    def create_server(*, transport: str, allowed_roots, api_base: str) -> Server:
        assert api_base == "https://api.mainbook.ai"
        modes.append(transport)
        return Server()

    monkeypatch.setattr(__main__, "create_server", create_server)

    __main__.main([])

    assert calls[0][0] == "streamable-http"
    assert calls[0][1]["port"] == 9001
    assert modes == ["http"]


def test_cli_positional_allowed_dirs_override_environment(monkeypatch, tmp_path, capsys) -> None:
    cli_root = tmp_path / "cli"
    env_root = tmp_path / "env"
    cli_root.mkdir()
    env_root.mkdir()
    missing_root = tmp_path / "missing"
    monkeypatch.setenv("MAINBOOK_ALLOWED_DIRS", str(env_root))
    captured: list[tuple[object, ...]] = []

    class Server:
        def run(self, transport, **kwargs) -> None:
            assert transport == "stdio"

    def create_server(*, transport: str, allowed_roots, api_base: str) -> Server:
        assert api_base == "https://api.mainbook.ai"
        assert transport == "stdio"
        captured.append(allowed_roots)
        return Server()

    monkeypatch.setattr(__main__, "create_server", create_server)

    __main__.main(["--transport", "stdio", str(cli_root), str(missing_root)])

    assert captured == [(cli_root.resolve(),)]
    stderr = capsys.readouterr().err
    assert str(cli_root.resolve()) in stderr
    assert str(env_root.resolve()) not in stderr
    assert str(missing_root) not in stderr


def test_cli_allowed_dirs_use_environment_before_defaults(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    default_root = home / "Downloads"
    default_root.mkdir(parents=True)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "MAINBOOK_ALLOWED_DIRS",
        os.pathsep.join((str(first), str(second))),
    )

    roots = __main__.resolve_allowed_roots(())

    assert roots == (first.resolve(), second.resolve())
    assert default_root.resolve() not in roots


def test_cli_allowed_dirs_use_existing_defaults_last(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    downloads = home / "Downloads"
    documents = home / "Documents"
    downloads.mkdir(parents=True)
    documents.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MAINBOOK_ALLOWED_DIRS", raising=False)

    roots = __main__.resolve_allowed_roots(())

    assert roots == (downloads.resolve(), documents.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth_enabled", [False, True])
async def test_standalone_get_is_refused_instead_of_hanging_forever(
    oauth_enabled: bool,
) -> None:
    """A GET must answer 405, not hold a stream that this server can never speak on.

    We run `stateless_http=True`, so there are no server-initiated messages, but the SDK
    still accepts GET and keeps the connection open. Codex CLI probes with GET during
    OAuth discovery and hangs there; the same hang reproduces on production.
    The spec: a server that offers no SSE stream at this endpoint MUST return 405.
    """
    oauth_settings = OAuthSettings(
        enabled=oauth_enabled,
        service_signing_secret="service-secret-for-test" if oauth_enabled else "",
    )
    server = create_server(
        transport="http",
        oauth_settings=oauth_settings,
        oauth_verifier=NeverVerifier() if oauth_enabled else None,  # type: ignore[arg-type]
    )
    port = free_port()
    task = asyncio.create_task(
        server.run_streamable_http_async(
            host="127.0.0.1",
            port=port,
            json_response=True,
            stateless_http=True,
        )
    )
    await wait_for_port(port)

    try:
        async with httpx2.AsyncClient(timeout=5.0) as http_client:
            refused = await http_client.get(
                f"http://127.0.0.1:{port}/mcp",
                headers={"Accept": "text/event-stream"},
            )
            assert refused.status_code == 405
            assert refused.headers["allow"] == "POST"

            # The discovery document is also a GET and must keep working.
            if oauth_enabled:
                metadata = await http_client.get(
                    f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp"
                )
                assert metadata.status_code == 200
                assert metadata.json()["resource"] == oauth_settings.resource
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _challenge_app(monkeypatch, token: str | None):
    if token is None:
        monkeypatch.delenv("MAINBOOK_MCP_OPENAI_APPS_CHALLENGE", raising=False)
    else:
        monkeypatch.setenv("MAINBOOK_MCP_OPENAI_APPS_CHALLENGE", token)
    return create_server(transport="http", oauth_settings=OAuthSettings()).streamable_http_app(
        stateless_http=True, json_response=True
    )


@pytest.mark.asyncio
async def test_openai_apps_challenge_is_absent_until_a_token_is_configured(monkeypatch) -> None:
    """An empty 200 would read as a wrong token; the path must simply not exist yet."""
    app = _challenge_app(monkeypatch, None)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        response = await client.get("/.well-known/openai-apps-challenge")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_openai_apps_challenge_returns_the_bare_token(monkeypatch) -> None:
    """OpenAI compares the whole body with the token, so nothing may wrap or follow it."""
    token = "oai-apps-challenge-EXAMPLE-0123456789"
    app = _challenge_app(monkeypatch, f"  {token}  ")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        response = await client.get("/.well-known/openai-apps-challenge")
        head = await client.head("/.well-known/openai-apps-challenge")
        posted = await client.post("/.well-known/openai-apps-challenge")

    assert response.status_code == 200
    assert response.text == token
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert head.status_code == 200
    assert posted.status_code == 405


@pytest.mark.asyncio
async def test_openai_apps_challenge_leaves_the_mcp_endpoint_alone(monkeypatch) -> None:
    """The challenge sits in front of everything, so prove it still passes MCP through."""
    app = _challenge_app(monkeypatch, "oai-apps-challenge-EXAMPLE-0123456789")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://mcp.mainbook.ai"
    ) as client:
        response = await client.get("/mcp")
    assert response.status_code == 405


def test_read_only_tools_declare_every_hint_the_directories_read() -> None:
    """OpenAI's review rejects tools whose readOnly/destructive/openWorld hints are absent."""
    annotations = server_module.READ_ANNOTATIONS
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True
    assert annotations.open_world_hint is True
