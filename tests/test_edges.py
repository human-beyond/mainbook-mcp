"""Defensive-edge tests derived from D8 task spec §§4-7 and design spec §5.6."""

from __future__ import annotations

import io
import os
import socket
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest
from mcp import Client
from pypdf import PdfWriter
from test_server import API_KEY, JOB_ID, FakeAPIClient, server_with_fake

from mainbook_mcp.client import (
    MainBookClient,
    _developer_base_url,
    _retry_after_seconds,
)
from mainbook_mcp.errors import (
    MainBookAPIError,
    MainBookError,
    MainBookFileError,
    MainBookNetworkError,
)
from mainbook_mcp.files import (
    _content_length,
    _require_public_addresses,
    _safe_filename,
    _url_filename,
    _validated_pdf_source,
    download_pdf_url,
    load_local_pdf,
    resolve_host_addresses,
)
from mainbook_mcp.models import ConvertBankStatementInput
from mainbook_mcp.server import (
    _api_key_for_request,
    _cursor_from_next,
    _default_client_factory,
    _default_source_loader,
    _header,
    _required_string,
    _result_endpoint,
)


def _pdf_bytes(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def _public_resolver(host: str, port: int) -> Sequence[str]:
    return ("93.184.216.34",)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["instructions", "connect", "rejected"])
async def test_upload_failures_are_sanitized(mode: str) -> None:
    """D8 task §§5-6: upload failures expose recovery guidance, not transport internals."""

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "connect":
            raise httpx.ConnectError("secret storage detail", request=request)
        return httpx.Response(403)

    async with MainBookClient(
        api_key=API_KEY,
        base_url="https://stub.mainbook.test",
        transport=httpx.MockTransport(handler),
    ) as api:
        upload: dict[str, Any]
        if mode == "instructions":
            upload = {"url": "https://storage.example/upload", "method": "POST", "headers": {}}
        else:
            upload = {"url": "https://storage.example/upload", "method": "PUT", "headers": {}}
        with pytest.raises(MainBookNetworkError) as raised:
            await api.upload_pdf(upload, b"%PDF")

    assert "secret storage detail" not in str(raised.value)


@pytest.mark.asyncio
async def test_client_rejects_unreadable_and_non_object_success_payloads() -> None:
    """Design spec §5.6: malformed success bodies fail closed with an agent-safe error."""
    responses = iter(
        [
            httpx.Response(200, content=b"not-json"),
            httpx.Response(200, json=["not", "an", "object"]),
        ]
    )
    async with MainBookClient(
        api_key=API_KEY,
        base_url="https://stub.mainbook.test",
        transport=httpx.MockTransport(lambda request: next(responses)),
    ) as api:
        with pytest.raises(MainBookNetworkError, match="unreadable"):
            await api.get_balance()
        with pytest.raises(MainBookNetworkError, match="unexpected"):
            await api.get_balance()


@pytest.mark.asyncio
async def test_client_handles_non_json_error_and_optional_list_cursor() -> None:
    """Design spec §§5.1, 5.6: list cursor is optional and non-JSON refusals are sanitized."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"results": [], "next": None, "previous": None})
        return httpx.Response(503, content=b"upstream HTML and internals")

    async with MainBookClient(
        api_key=API_KEY,
        base_url="https://stub.mainbook.test/api/v1",
        transport=httpx.MockTransport(handler),
    ) as api:
        await api.list_jobs(limit=25)
        with pytest.raises(MainBookAPIError) as raised:
            await api.get_balance()

    assert dict(requests[0].url.params) == {"page_size": "25"}
    assert raised.value.reason == "service_error"
    assert "upstream HTML" not in str(raised.value)


@pytest.mark.asyncio
async def test_poller_propagates_non_rate_error_and_handles_deadlines() -> None:
    """D8 task §§3.1, 5: only 429 is retried and every deadline path remains resumable."""

    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"reason": "engine_paused"})

    immediate_clock = iter([0.0, 30.0])
    async with MainBookClient(
        api_key=API_KEY,
        transport=httpx.MockTransport(rejected),
        monotonic=lambda: next(immediate_clock),
    ) as api:
        outcome = await api.poll_job(JOB_ID, timeout_seconds=30)
    assert outcome.timed_out is True

    async with MainBookClient(
        api_key=API_KEY,
        transport=httpx.MockTransport(rejected),
        monotonic=lambda: 0.0,
    ) as api:
        with pytest.raises(MainBookAPIError, match="temporarily paused"):
            await api.poll_job(JOB_ID, timeout_seconds=30)

    clock = iter([0.0, 0.0, 30.0])
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"reason": "rate_limited"})

    async with MainBookClient(
        api_key=API_KEY,
        transport=httpx.MockTransport(limited),
        monotonic=lambda: next(clock),
        sleep=sleep,
    ) as api:
        outcome = await api.poll_job(JOB_ID, timeout_seconds=30)
    assert outcome.timed_out is True
    assert sleeps == []


def test_client_url_and_retry_after_helpers_cover_public_variants() -> None:
    """D8 task §§4-5: base URLs and both legal Retry-After forms normalize deterministically."""
    assert _developer_base_url("https://example.test/api/v1/developer/") == (
        "https://example.test/api/v1/developer"
    )
    assert (
        _developer_base_url("https://example.test/api/v1")
        == "https://example.test/api/v1/developer"
    )
    assert _developer_base_url("https://example.test") == "https://example.test/api/v1/developer"

    now = datetime(2026, 8, 10, tzinfo=UTC)
    assert _retry_after_seconds(None, now=now) is None
    assert _retry_after_seconds("invalid", now=now) is None
    assert _retry_after_seconds("Sun, 10 Aug 2026 00:00:05", now=now) == 5
    assert _retry_after_seconds("-3", now=now) == 0


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("job_not_terminal", "not finished"),
        ("account_suspended", "suspended"),
        ("not_found", "not found"),
        ("invalid_request", "invalid"),
        ("unknown_reason", "HTTP 418"),
    ],
)
@pytest.mark.asyncio
async def test_remaining_named_api_errors_are_actionable(reason: str, expected: str) -> None:
    """D8 task §5: every documented reason maps to an action-oriented safe message."""
    async with MainBookClient(
        api_key=API_KEY,
        transport=httpx.MockTransport(lambda request: httpx.Response(418, json={"reason": reason})),
    ) as api:
        with pytest.raises(MainBookAPIError) as raised:
            await api.get_balance()
    assert expected in str(raised.value)


@pytest.mark.asyncio
async def test_local_read_race_and_actual_size_are_bounded(tmp_path, monkeypatch) -> None:
    """D8 task §4: descriptor reads stay bounded and open errors are sanitized."""
    path = tmp_path / "statement.pdf"
    path.write_bytes(b"123456")

    monkeypatch.setattr(
        os,
        "fstat",
        lambda fd: SimpleNamespace(st_mode=stat.S_IFREG, st_size=1),
    )
    with pytest.raises(MainBookFileError, match="larger than"):
        await load_local_pdf(str(path), allowed_roots=(tmp_path,), max_bytes=5)

    def unreadable(path: Path, flags: int) -> int:
        raise OSError("private filesystem details")

    monkeypatch.setattr(os, "open", unreadable)
    with pytest.raises(MainBookFileError, match="cannot be read") as raised:
        await load_local_pdf(str(path), allowed_roots=(tmp_path,))
    assert "private filesystem" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test:bad/a.pdf", "valid HTTPS"),
        ("https://user:pass@example.test/a.pdf", "without embedded credentials"),
        ("https:///a.pdf", "public HTTPS"),
    ],
)
async def test_remote_url_shape_is_rejected(url: str, expected: str) -> None:
    """D8 task §4 SSRF guard: malformed ports, credentials and missing hosts fail before I/O."""
    with pytest.raises(MainBookFileError, match=expected):
        await download_pdf_url(url, resolver=_public_resolver)


@pytest.mark.asyncio
async def test_remote_dns_and_http_failures_are_sanitized() -> None:
    """D8 task §§4-6: DNS, HTTP status and connection failures are distinct safe errors."""

    async def broken_resolver(host: str, port: int) -> Sequence[str]:
        raise socket.gaierror("private resolver detail")

    with pytest.raises(MainBookFileError, match="could not be resolved") as raised:
        await download_pdf_url("https://example.test/a.pdf", resolver=broken_resolver)
    assert "private resolver" not in str(raised.value)

    with pytest.raises(MainBookFileError, match="link is unavailable") as raised:
        await download_pdf_url(
            "https://example.test/a.pdf",
            resolver=_public_resolver,
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        )
    assert "404" not in str(raised.value)
    assert "example.test" not in str(raised.value)

    def broken_http(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private socket detail", request=request)

    with pytest.raises(MainBookFileError, match="could not be reached") as raised:
        await download_pdf_url(
            "https://example.test/a.pdf",
            resolver=_public_resolver,
            transport=httpx.MockTransport(broken_http),
        )
    assert "private socket" not in str(raised.value)


@pytest.mark.asyncio
async def test_resolver_and_pdf_filename_helpers_fail_closed(monkeypatch) -> None:
    """D8 task §4 SSRF guard: every DNS answer and generated filename is validated."""
    records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: records)
    assert await resolve_host_addresses("example.test", 443) == ("93.184.216.34",)

    def dns_failure(*args: object) -> object:
        raise socket.gaierror("no answer")

    monkeypatch.setattr(socket, "getaddrinfo", dns_failure)
    with pytest.raises(socket.gaierror):
        await resolve_host_addresses("example.test", 443)

    for addresses in ((), ("not-an-ip",)):
        with pytest.raises(MainBookFileError, match="public Internet"):
            _require_public_addresses(addresses)

    assert _content_length(None) is None
    assert _content_length("not-a-number") is None
    assert _content_length("-1") is None
    assert _url_filename("/") == "statement.pdf"
    assert _safe_filename(" /\\\x00 ") == "statement.pdf"
    assert len(_safe_filename("x" * 300)) == 255


@pytest.mark.asyncio
async def test_empty_pdf_is_rejected() -> None:
    """Design spec §5.2: a readable PDF must contain at least one statement page."""
    with pytest.raises(MainBookFileError, match="no pages"):
        await _validated_pdf_source(filename="empty.pdf", data=_pdf_bytes(0), max_pages=500)


@pytest.mark.asyncio
async def test_server_idempotency_replay_paths(monkeypatch) -> None:
    """D8 task §§3.1: idempotency replay resumes a job or rejects an expired upload safely."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)

    class ReplayAPI(FakeAPIClient):
        async def create_job(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("create_job", kwargs))
            return {"job_id": JOB_ID, "upload": None, "credits_reserved": 2}

    running = ReplayAPI(state="processing", timed_out=True)
    async with Client(server_with_fake(running)) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/tmp/statement.pdf", "timeout_seconds": 30},
        )
    assert result.is_error is not True
    assert [name for name, _ in running.calls] == ["create_job", "get_job", "poll_job"]

    expired = ReplayAPI(state="expired")
    async with Client(server_with_fake(expired)) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/tmp/statement.pdf", "timeout_seconds": 30},
        )
    assert result.is_error is True
    assert "new idempotency key" in result.content[0].text


@pytest.mark.asyncio
async def test_server_rejects_malformed_rest_objects(monkeypatch) -> None:
    """D8 task §§5-6: invalid create, balance, job list and export payloads fail closed."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)

    class MissingJobID(FakeAPIClient):
        async def create_job(self, **kwargs: object) -> dict[str, object]:
            return {"upload": None}

    async with Client(server_with_fake(MissingJobID())) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/tmp/statement.pdf", "timeout_seconds": 30},
        )
    assert result.is_error is True
    assert "create-job response" in result.content[0].text

    for bad_balance in ({"balance": 1}, {"balance": "one", "reserved": 0, "available": 1}):
        async with Client(server_with_fake(FakeAPIClient(balance=bad_balance))) as client:  # type: ignore[arg-type]
            result = await client.call_tool("get_balance", {})
        assert result.is_error is True
        assert "unexpected balance" in result.content[0].text

    for bad_page in ({"results": "not-list"}, {"results": [{"job_id": JOB_ID}]}):
        async with Client(server_with_fake(FakeAPIClient(page=bad_page))) as client:
            result = await client.call_tool("list_conversions", {})
        assert result.is_error is True
        assert "unexpected" in result.content[0].text

    async with Client(server_with_fake(FakeAPIClient(result={"not": "an export"}))) as client:
        result = await client.call_tool("get_conversion", {"job_id": JOB_ID})
    assert result.is_error is True
    assert "unexpected JSON export" in result.content[0].text

    class InvalidJob(FakeAPIClient):
        async def get_job(self, job_id: str) -> dict[str, object]:
            return {"job_id": job_id, "state": "succeeded"}

    async with Client(server_with_fake(InvalidJob())) as client:
        result = await client.call_tool("get_conversion", {"job_id": JOB_ID})
    assert result.is_error is True
    assert "unexpected job response" in result.content[0].text


@pytest.mark.asyncio
async def test_nonterminal_get_conversion_is_status_only(monkeypatch) -> None:
    """D8 task §3.4: a queued job returns status and does not fetch a premature result."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient(state="queued")
    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool("get_conversion", {"job_id": JOB_ID})
    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["state"] == "queued"
    assert "get_conversion again" in result.structured_content["message"]
    assert [name for name, _ in fake.calls] == ["get_job"]


@pytest.mark.asyncio
async def test_default_source_loader_selects_local_and_remote(monkeypatch) -> None:
    """D8 task §§3.1, 4: source choice dispatches once and preserves the validated input."""
    source = object()
    calls: list[tuple[str, str]] = []

    async def local(value: str) -> object:
        calls.append(("local", value))
        return source

    async def remote(value: str) -> object:
        calls.append(("remote", value))
        return source

    monkeypatch.setattr("mainbook_mcp.server.load_local_pdf", local)
    monkeypatch.setattr("mainbook_mcp.server.download_pdf_url", remote)
    assert await _default_source_loader(ConvertBankStatementInput(file_path="/tmp/a.pdf")) is source
    assert (
        await _default_source_loader(
            ConvertBankStatementInput(file_url="https://example.test/a.pdf")
        )
        is source
    )
    assert calls == [("local", "/tmp/a.pdf"), ("remote", "https://example.test/a.pdf")]

    impossible = ConvertBankStatementInput.model_construct(file_path=None, file_url=None)
    with pytest.raises(MainBookError, match="Exactly one PDF source"):
        await _default_source_loader(impossible)


def test_server_pure_helpers_cover_contract_variants() -> None:
    """D8 task §§3-4: cursors, endpoints, required fields and factories normalize safely."""
    assert isinstance(_default_client_factory(API_KEY, "https://example.test"), MainBookClient)
    assert _required_string({"job_id": JOB_ID}, "job_id") == JOB_ID
    for payload in ({}, {"job_id": ""}, {"job_id": 3}):
        with pytest.raises(MainBookNetworkError):
            _required_string(payload, "job_id")

    assert _cursor_from_next(None) is None
    assert _cursor_from_next("https://example.test/jobs?page_size=2") is None
    assert _result_endpoint("https://example.test/api/v1/developer", JOB_ID, "csv").endswith(
        f"/developer/jobs/{JOB_ID}/result?type=csv"
    )
    assert _result_endpoint("https://example.test/api/v1", JOB_ID, "xlsx").endswith(
        f"/developer/jobs/{JOB_ID}/result?type=xlsx"
    )
    assert _result_endpoint("https://example.test", JOB_ID, "json").endswith(
        f"/developer/jobs/{JOB_ID}/result?type=json"
    )

    with pytest.raises(MainBookError, match="non-empty Bearer"):
        _api_key_for_request(  # type: ignore[arg-type]
            SimpleNamespace(headers={"Authorization": "Basic not-a-bearer"})
        )
    assert _header({"X-Other": "value"}, "authorization") is None
