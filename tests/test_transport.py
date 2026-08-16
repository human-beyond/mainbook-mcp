"""Transport tests derived from D8 task spec §§4, 6 and 7."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mainbook_mcp import __main__
from mainbook_mcp.server import create_server

ENV_KEY = "mb_live_environment_fallback"
FIRST_KEY = "mb_live_http_first_request"
SECOND_KEY = "mb_live_http_second_request"


class BalanceClient:
    async def get_balance(self) -> dict[str, int]:
        return {"balance": 10, "reserved": 1, "available": 9}


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
async def test_http_header_key_overrides_environment_and_never_leaks_between_requests(
    monkeypatch,
) -> None:
    """D8 task spec §§4 and 7: every HTTP tool call uses its own Authorization header."""
    monkeypatch.setenv("MAINBOOK_API_KEY", ENV_KEY)
    captured_keys: list[str] = []

    @asynccontextmanager
    async def factory(api_key: str, base_url: str) -> AsyncIterator[BalanceClient]:
        captured_keys.append(api_key)
        yield BalanceClient()

    server = create_server(transport="http", client_factory=factory)
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


def test_cli_defaults_to_stdio(monkeypatch) -> None:
    """D8 task spec §4: mainbook-mcp without flags is the local stdio server."""
    calls: list[tuple[str, dict[str, object]]] = []
    modes: list[str] = []

    class Server:
        def run(self, transport, **kwargs) -> None:
            calls.append((transport, kwargs))

    def create_server(*, transport: str, allowed_roots) -> Server:
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

    def create_server(*, transport: str, allowed_roots) -> Server:
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

    def create_server(*, transport: str, allowed_roots) -> Server:
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

    def create_server(*, transport: str, allowed_roots) -> Server:
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
