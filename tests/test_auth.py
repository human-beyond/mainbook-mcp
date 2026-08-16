"""Browser-assisted device authorization contract."""

from __future__ import annotations

import base64
import hashlib
import json
import re

import httpx2 as httpx
import pytest

from mainbook_mcp import auth as auth_module
from mainbook_mcp.auth import (
    AuthFlowError,
    DeviceAuthFlow,
    DeviceToken,
    check_credential,
    create_pkce_pair,
    detect_client_name,
    revoke_credential,
)

API_KEY = "mb_live_device_flow_secret_never_print"
DEVICE_CODE = "D" * 43
VERIFICATION_URI = "https://mainbook.ai/connect?code=ABCD2345X"


@pytest.mark.asyncio
async def test_credential_lifecycle_uses_the_free_self_endpoint() -> None:
    requests: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(200, json={"active": True, "client_name": "Terminal"}),
            httpx.Response(204),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    transport = httpx.MockTransport(handler)

    assert await check_credential(
        api_base="https://api.mainbook.ai", api_key=API_KEY, transport=transport
    )
    assert await revoke_credential(
        api_base="https://api.mainbook.ai", api_key=API_KEY, transport=transport
    )
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/developer/key"),
        ("DELETE", "/api/v1/developer/key"),
    ]
    assert all(request.headers["Authorization"] == f"Bearer {API_KEY}" for request in requests)


@pytest.mark.asyncio
async def test_revoked_credential_status_and_logout_are_idempotent() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401))

    assert not await check_credential(
        api_base="https://api.mainbook.ai", api_key=API_KEY, transport=transport
    )
    assert not await revoke_credential(
        api_base="https://api.mainbook.ai", api_key=API_KEY, transport=transport
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def start_response(*, expires_in: int = 600, interval: int = 2) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "device_code": DEVICE_CODE,
            "verification_uri": VERIFICATION_URI,
            "expires_in": expires_in,
            "interval": interval,
        },
    )


def test_pkce_pair_has_exact_shape_and_s256_relationship() -> None:
    verifier, challenge = create_pkce_pair()

    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", verifier)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode("ascii")


@pytest.mark.asyncio
async def test_login_posts_exact_contract_opens_browser_and_polls_at_server_interval() -> None:
    requests: list[httpx.Request] = []
    responses = iter(
        [
            start_response(),
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"api_key": API_KEY, "client_name": "Claude Code on Mac"}),
        ]
    )
    clock = Clock()
    opened: list[str] = []
    output: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        open_browser=lambda url: opened.append(url) or True,
        write=output.append,
    )
    result = await flow.run(client_name="Claude Code on Mac")

    assert result.api_key == API_KEY
    assert result.client_name == "Claude Code on Mac"
    assert opened == [VERIFICATION_URI]
    assert clock.sleeps == [2.0, 2.0, 7.0]
    assert requests[0].url.path == "/api/v1/developer/device/start"
    start_body = json.loads(requests[0].content)
    assert start_body["client_name"] == "Claude Code on Mac"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", start_body["code_challenge"])
    token_bodies = [json.loads(request.content) for request in requests[1:]]
    assert all(body["device_code"] == DEVICE_CODE for body in token_bodies)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", body["code_verifier"]) for body in token_bodies)
    assert API_KEY not in "\n".join(output)
    assert any("ABCD2345X" in line for line in output)


@pytest.mark.asyncio
async def test_no_browser_prints_url_without_calling_browser() -> None:
    responses = iter(
        [
            start_response(),
            httpx.Response(200, json={"api_key": API_KEY, "client_name": "Terminal"}),
        ]
    )
    clock = Clock()
    output: list[str] = []
    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(lambda request: next(responses)),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        open_browser=lambda url: pytest.fail("browser must not open"),
        write=output.append,
    )

    result = await flow.run(client_name="Terminal", no_browser=True)

    assert result.api_key == API_KEY
    assert VERIFICATION_URI in "\n".join(output)
    assert API_KEY not in "\n".join(output)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol_error", "message"),
    [
        ("access_denied", "denied"),
        ("expired_token", "expired"),
    ],
)
async def test_terminal_protocol_errors_are_actionable_and_sanitized(
    protocol_error: str, message: str
) -> None:
    responses = iter([start_response(), httpx.Response(400, json={"error": protocol_error})])
    clock = Clock()
    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(lambda request: next(responses)),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        open_browser=lambda url: True,
        write=lambda line: None,
    )

    with pytest.raises(AuthFlowError, match=message) as raised:
        await flow.run(client_name="Terminal")

    assert API_KEY not in str(raised.value)


@pytest.mark.asyncio
async def test_local_deadline_stops_without_polling_past_expires_in() -> None:
    requests: list[httpx.Request] = []
    responses = iter(
        [
            start_response(expires_in=3, interval=2),
            httpx.Response(400, json={"error": "authorization_pending"}),
        ]
    )
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    flow = DeviceAuthFlow(
        api_base="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        open_browser=lambda url: True,
        write=lambda line: None,
    )

    with pytest.raises(AuthFlowError, match=r"expired.*again"):
        await flow.run(client_name="Terminal")

    assert clock.sleeps == [2.0, 1.0]
    assert len(requests) == 2


def test_client_name_is_honest_and_truncated() -> None:
    assert (
        detect_client_name(environ={"CLAUDECODE": "1"}, hostname="Vadims-MacBook")
        == "Claude Code on Vadims-MacBook"
    )
    assert detect_client_name(environ={}, hostname="host") == "MainBook MCP on host"
    assert len(detect_client_name(environ={}, hostname="x" * 150)) == 100


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"CODEX_THREAD_ID": "thread"}, "Codex on host"),
        ({"CURSOR_TRACE_ID": "trace"}, "Cursor on host"),
        ({"VSCODE_PID": "123"}, "VS Code on host"),
        ({"TERM_PROGRAM": "iTerm.app"}, "iTerm on host"),
    ],
)
def test_client_name_recognizes_common_terminal_clients(
    environ: dict[str, str], expected: str
) -> None:
    assert detect_client_name(environ=environ, hostname="host") == expected


@pytest.mark.asyncio
async def test_blank_client_name_stops_before_network() -> None:
    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(lambda request: pytest.fail("network was reached")),
    )

    with pytest.raises(AuthFlowError, match="client name"):
        await flow.run(client_name="\x00\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(429, json={"detail": "throttled"}), "Too many"),
        (httpx.Response(400, json={"code_challenge": ["invalid"]}), "could not start"),
    ],
)
async def test_start_errors_are_actionable(response: httpx.Response, expected: str) -> None:
    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(lambda request: response),
    )

    with pytest.raises(AuthFlowError, match=expected):
        await flow.run(client_name="Terminal")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"device_code": "short"}),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(
            200,
            json={
                "device_code": DEVICE_CODE,
                "verification_uri": "http://remote.example/connect?code=ABCD2345X",
                "expires_in": 600,
                "interval": 2,
            },
        ),
        httpx.Response(
            200,
            json={
                "device_code": DEVICE_CODE,
                "verification_uri": "https://[",
                "expires_in": 600,
                "interval": 2,
            },
        ),
    ],
)
async def test_invalid_start_payload_is_sanitized(response: httpx.Response) -> None:
    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(lambda request: response),
    )

    with pytest.raises(AuthFlowError, match=r"response|URL"):
        await flow.run(client_name="Terminal")


@pytest.mark.asyncio
async def test_browser_failure_prints_url_and_continues() -> None:
    responses = iter(
        [
            start_response(),
            httpx.Response(200, json={"api_key": API_KEY, "client_name": "Terminal"}),
        ]
    )
    clock = Clock()
    output: list[str] = []

    def refuse_browser(url: str) -> bool:
        raise RuntimeError("no browser")

    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(lambda request: next(responses)),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        open_browser=refuse_browser,
        write=output.append,
    )

    await flow.run(client_name="Terminal")

    assert VERIFICATION_URI in "\n".join(output)
    assert API_KEY not in "\n".join(output)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_payload",
    [
        {"api_key": "", "client_name": "Terminal"},
        {"api_key": API_KEY, "client_name": ""},
    ],
)
async def test_incomplete_token_response_is_never_rendered(token_payload: dict[str, str]) -> None:
    responses = iter([start_response(), httpx.Response(200, json=token_payload)])
    clock = Clock()
    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(lambda request: next(responses)),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        open_browser=lambda url: True,
        write=lambda line: None,
    )

    with pytest.raises(AuthFlowError, match="incomplete") as raised:
        await flow.run(client_name="Terminal")

    assert API_KEY not in str(raised.value)


@pytest.mark.asyncio
async def test_unexpected_token_error_is_sanitized() -> None:
    responses = iter([start_response(), httpx.Response(503, json={"detail": API_KEY})])
    clock = Clock()
    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(lambda request: next(responses)),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        open_browser=lambda url: True,
        write=lambda line: None,
    )

    with pytest.raises(AuthFlowError, match="could not complete") as raised:
        await flow.run(client_name="Terminal")

    assert API_KEY not in str(raised.value)


@pytest.mark.asyncio
async def test_network_error_does_not_expose_transport_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private transport detail", request=request)

    flow = DeviceAuthFlow(
        api_base="https://mainbook.ai",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AuthFlowError, match="Could not reach") as raised:
        await flow.run(client_name="Terminal")

    assert "private transport detail" not in str(raised.value)


@pytest.mark.asyncio
async def test_perform_login_wrapper_forwards_options(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []

    class FakeFlow:
        def __init__(self, *, api_base: str, write) -> None:
            assert api_base == "https://mainbook.ai"

        async def run(self, *, client_name: str, no_browser: bool) -> DeviceToken:
            calls.append((client_name, no_browser))
            return DeviceToken(api_key=API_KEY, client_name=client_name)

    monkeypatch.setattr(auth_module, "DeviceAuthFlow", FakeFlow)

    result = await auth_module.perform_login(
        api_base="https://mainbook.ai",
        client_name="Terminal",
        no_browser=True,
    )

    assert result.client_name == "Terminal"
    assert calls == [("Terminal", True)]
