"""REST client tests derived from design spec §§5.1-5.6, 8.5 and D8 task §3."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx2 as httpx
import pytest

from mainbook_mcp.client import MainBookClient
from mainbook_mcp.errors import MainBookAPIError, MainBookNetworkError

API_KEY = "mb_live_test_material_never_real"
JOB_ID = "00000000-0000-0000-0000-000000000001"


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: object,
) -> MainBookClient:
    return MainBookClient(
        api_key=API_KEY,
        base_url="https://stub.mainbook.test",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_create_upload_start_preserves_contract_and_presigned_headers() -> None:
    """Design spec §§5.1-5.2: create metadata, verbatim PUT headers, then empty start."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/jobs"):
            return httpx.Response(
                201,
                json={
                    "job_id": JOB_ID,
                    "upload": {
                        "url": "https://storage.example/upload-token",
                        "method": "PUT",
                        "headers": {
                            "Content-Type": "application/pdf",
                            "X-Exact-Signed-Header": "value  with  spaces",
                        },
                        "expires_at": "2026-08-10T12:15:00Z",
                    },
                    "credits_reserved": 2,
                },
            )
        if request.url.host == "storage.example":
            return httpx.Response(200)
        return httpx.Response(200, json={"job_id": JOB_ID, "state": "queued"})

    async with make_client(handler) as client:
        created = await client.create_job(
            filename="statement.pdf",
            size_bytes=123,
            page_count=2,
            idempotency_key="idem-1",
        )
        await client.upload_pdf(created["upload"], b"%PDF-test")
        await client.start_job(JOB_ID)

    create, upload, start = requests
    assert create.method == "POST"
    assert create.url.path == "/api/v1/developer/jobs"
    assert create.headers["authorization"] == f"Bearer {API_KEY}"
    assert create.headers["idempotency-key"] == "idem-1"
    assert create.read() == (
        b'{"filename":"statement.pdf","file_format":"pdf","size_bytes":123,"page_count":2}'
    )

    assert upload.method == "PUT"
    assert upload.read() == b"%PDF-test"
    assert upload.headers["content-type"] == "application/pdf"
    assert upload.headers["x-exact-signed-header"] == "value  with  spaces"
    assert "authorization" not in upload.headers

    assert start.method == "POST"
    assert start.url.path == f"/api/v1/developer/jobs/{JOB_ID}/start"
    assert start.read() == b""


@pytest.mark.asyncio
async def test_read_methods_use_type_and_cursor_contract() -> None:
    """Design spec §5.1: result uses type; list uses page_size and opaque cursor."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"document": {}, "transactions": []})
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"balance": 20, "reserved": 2, "available": 18})
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"results": [], "next": None, "previous": None})
        return httpx.Response(200, json={"job_id": JOB_ID, "state": "processing"})

    async with make_client(handler) as client:
        await client.get_job(JOB_ID)
        await client.list_jobs(limit=40, cursor="opaque+/=")
        await client.get_result(JOB_ID, "json")
        await client.get_balance()

    assert seen[0].url.path.endswith(f"/jobs/{JOB_ID}")
    assert dict(seen[1].url.params) == {"page_size": "40", "cursor": "opaque+/="}
    assert dict(seen[2].url.params) == {"type": "json"}
    assert "format" not in seen[2].url.params
    assert seen[3].url.path.endswith("/balance")
    assert all(request.headers["authorization"] == f"Bearer {API_KEY}" for request in seen)


@pytest.mark.asyncio
async def test_binary_result_is_returned_as_bytes_only_at_rest_layer() -> None:
    """D8 task spec §3.1: REST client may receive binary, MCP tool must decide not to inline it."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"type": "xlsx"}
        return httpx.Response(200, content=b"PK\x03\x04workbook")

    async with make_client(handler) as client:
        result = await client.get_result(JOB_ID, "xlsx")

    assert result == b"PK\x03\x04workbook"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"detail": "invalid_key"}, "invalid or revoked"),
        (
            402,
            {"reason": "insufficient_credits", "available": 2, "requested": 5},
            "2 pages available",
        ),
        (403, {"reason": "api_terms_not_accepted"}, "mainbook.ai/developer"),
        (409, {"reason": "job_expired"}, "Create a new conversion"),
        (409, {"reason": "idempotency_conflict"}, "different request"),
        (429, {"reason": "concurrency_limit"}, "retry after 7"),
        (503, {"reason": "engine_paused"}, "temporarily paused"),
    ],
)
async def test_api_errors_are_actionable_and_sanitized(
    status: int,
    payload: dict[str, object],
    expected: str,
) -> None:
    """D8 task spec §5: named REST errors become actionable agent-safe messages."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers={"Retry-After": "7"})

    async with make_client(handler) as client:
        with pytest.raises(MainBookAPIError) as raised:
            await client.get_balance()

    assert expected in str(raised.value)
    assert API_KEY not in str(raised.value)
    assert raised.value.reason == ("invalid_key" if status == 401 else payload["reason"])


@pytest.mark.asyncio
async def test_network_failure_is_distinct_from_service_rejection() -> None:
    """D8 task spec §5: network failure must be distinguishable from an API refusal."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dial failed with secret-looking internals", request=request)

    async with make_client(handler) as client:
        with pytest.raises(MainBookNetworkError, match="could not reach") as raised:
            await client.get_balance()

    assert "dial failed" not in str(raised.value)
    assert API_KEY not in str(raised.value)


@pytest.mark.asyncio
async def test_poller_uses_retry_after_instead_of_constant_sleep() -> None:
    """Design spec §8.5 and D8 task §7: 429 Retry-After controls the poll delay."""
    responses = iter(
        [
            httpx.Response(429, json={"reason": "rate_limited"}, headers={"Retry-After": "8"}),
            httpx.Response(200, json={"job_id": JOB_ID, "state": "succeeded"}),
        ]
    )
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with make_client(handler, sleep=sleep, monotonic=lambda: 0.0) as client:
        outcome = await client.poll_job(JOB_ID, timeout_seconds=30)

    assert outcome.job["state"] == "succeeded"
    assert sleeps == [8.0]


@pytest.mark.asyncio
async def test_poller_parses_http_date_retry_after() -> None:
    """RFC Retry-After used by D8 task §5 may be either seconds or an HTTP date."""
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    retry_at = now + timedelta(seconds=11)
    responses = iter(
        [
            httpx.Response(
                429,
                json={"reason": "rate_limited"},
                headers={"Retry-After": retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT")},
            ),
            httpx.Response(200, json={"job_id": JOB_ID, "state": "failed"}),
        ]
    )
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with make_client(
        lambda request: next(responses),
        sleep=sleep,
        monotonic=lambda: 0.0,
        wall_clock=lambda: now,
    ) as client:
        await client.poll_job(JOB_ID, timeout_seconds=30)

    assert sleeps == [11.0]


@pytest.mark.asyncio
async def test_poller_stays_within_timeout_and_returns_job_id() -> None:
    """D8 task spec §§3.1 and 7: timeout is a resumable response, not an exception."""
    clock_values = iter([0.0, 0.0, 4.0, 8.0, 10.0])
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job_id": JOB_ID, "state": "processing"})

    async with make_client(
        handler,
        sleep=sleep,
        monotonic=lambda: next(clock_values),
        jitter=lambda low, high: 4.0,
    ) as client:
        outcome = await client.poll_job(JOB_ID, timeout_seconds=10)

    assert outcome.timed_out is True
    assert outcome.job == {"job_id": JOB_ID, "state": "processing"}
    assert sum(sleeps) <= 10


@pytest.mark.asyncio
async def test_poller_distinguishes_no_snapshot_from_confirmed_processing() -> None:
    """Review C: an immediate timeout must not invent a processing API snapshot."""
    clock_values = iter([0.0, 30.0])
    requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, json={"job_id": JOB_ID, "state": "processing"})

    async with make_client(handler, monotonic=lambda: next(clock_values)) as client:
        outcome = await client.poll_job(JOB_ID, timeout_seconds=30)

    assert outcome.timed_out is True
    assert outcome.job is None
    assert requested is False


@pytest.mark.asyncio
async def test_missing_api_key_is_rejected_before_network_io() -> None:
    """D8 task spec §§4 and 6: calls without a key fail clearly and locally."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(ValueError, match="API key is required"):
        MainBookClient(
            api_key="",
            base_url="https://stub.mainbook.test",
            transport=httpx.MockTransport(handler),
        )
    assert called is False


@pytest.mark.asyncio
async def test_poller_streams_progress_with_elapsed_time_and_state() -> None:
    """Progress is what lets a client extend its own request timeout instead of aborting.

    Claude Desktop 1.26832.0 cuts an MCP request at 60 s; a 151-second conversion observed on
    2026-08-11 died there while the job kept running and the job_id never reached the user.
    """
    clock_values = iter([0.0, 0.0, 4.0, 8.0, 10.0])
    reported: list[tuple[float, str]] = []

    async def sleep(seconds: float) -> None:
        return None

    async def on_progress(elapsed: float, state: str) -> None:
        reported.append((elapsed, state))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job_id": JOB_ID, "state": "processing"})

    async with make_client(
        handler,
        sleep=sleep,
        monotonic=lambda: next(clock_values),
        jitter=lambda low, high: 4.0,
    ) as client:
        await client.poll_job(JOB_ID, timeout_seconds=10, on_progress=on_progress)

    assert reported, "the poller must report progress while the job is still running"
    assert [state for _, state in reported] == ["processing"] * len(reported)
    assert [elapsed for elapsed, _ in reported] == sorted(elapsed for elapsed, _ in reported)


@pytest.mark.asyncio
async def test_progress_failure_never_breaks_a_running_conversion() -> None:
    """A client that rejects progress must not turn a paid, running job into an error.

    The guarantee lives in the server's reporter, not in the poller: the poller simply awaits
    whatever it is given, so the swallow has to be tested where it actually is.
    """
    from mainbook_mcp.server import _progress_reporter

    class RefusingContext:
        async def report_progress(self, **kwargs: object) -> None:
            raise RuntimeError("this client does not accept progress notifications")

    report = _progress_reporter(RefusingContext(), 50)

    await report(1.0, "processing")  # must not raise


@pytest.mark.asyncio
async def test_api_calls_announce_this_server_but_the_storage_put_does_not() -> None:
    """The backend labels the conversion channel from this token.

    It is attribution, not authentication — the backend normalises the leading
    token and grants nothing on it. The presigned storage PUT is signed for a
    bare request, so our marker must stay off it.
    """
    from mainbook_mcp import __version__
    from mainbook_mcp.client import USER_AGENT

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/jobs"):
            return httpx.Response(
                201,
                json={
                    "job_id": JOB_ID,
                    "upload": {
                        "url": "https://storage.example/upload-token",
                        "method": "PUT",
                        "headers": {"Content-Type": "application/pdf"},
                        "expires_at": "2026-08-10T12:15:00Z",
                    },
                    "credits_reserved": 1,
                },
            )
        return httpx.Response(200)

    async with make_client(handler) as client:
        created = await client.create_job(filename="s.pdf", size_bytes=1, page_count=1)
        await client.upload_pdf(created["upload"], b"%PDF-test")

    create, upload = requests
    assert f"mainbook-mcp/{__version__}" == USER_AGENT
    assert create.headers["user-agent"] == USER_AGENT
    assert "mainbook-mcp" not in upload.headers.get("user-agent", "")


@pytest.mark.asyncio
async def test_a_caller_supplied_header_cannot_drop_our_identification() -> None:
    """Per-call headers (Idempotency-Key) merge in; they do not replace the set."""
    from mainbook_mcp.client import USER_AGENT

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"job_id": JOB_ID})

    async with make_client(handler) as client:
        await client.create_job(
            filename="s.pdf", size_bytes=1, page_count=1, idempotency_key="idem-1"
        )

    assert seen[0].headers["user-agent"] == USER_AGENT
    assert seen[0].headers["idempotency-key"] == "idem-1"
    assert seen[0].headers["authorization"] == f"Bearer {API_KEY}"
