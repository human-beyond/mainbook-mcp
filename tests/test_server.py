"""MCP tool tests derived from D8 task spec §§3-7 and live REST output shape."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import Client
from pypdf import PdfWriter

from mainbook_mcp import server as server_module
from mainbook_mcp.client import PollOutcome
from mainbook_mcp.credentials import StoredCredential
from mainbook_mcp.errors import MainBookAPIError
from mainbook_mcp.files import PDFSource
from mainbook_mcp.server import create_server

API_KEY = "mb_live_test_server_key_material"
SECOND_KEY = "mb_live_second_request_key"
JOB_ID = "00000000-0000-0000-0000-000000000001"


def job(state: str = "succeeded") -> dict[str, Any]:
    successful = state in {"succeeded", "succeeded_with_warnings"}
    return {
        "job_id": JOB_ID,
        "state": state,
        "filename": "statement.pdf",
        "file_format": "pdf",
        "pages": 2,
        "credits_reserved": None,
        "source": "api",
        "created_at": "2026-08-10T12:00:00Z",
        "updated_at": "2026-08-10T12:00:10Z",
        "validation": (
            {"reconcilable": True, "passed": state == "succeeded", "mismatched_rows": 0}
            if successful
            else None
        ),
        "error": (
            {
                "category": "internal_category_must_not_escape",
                "reason": "internal_reason_must_not_escape",
            }
            if state == "failed"
            else None
        ),
    }


def conversion_data() -> dict[str, Any]:
    return {
        "document": {
            "id": JOB_ID,
            "display_name": "statement.pdf",
            "bank_name": "Example Bank",
            "account_holder": "A. Customer",
            "account_address": "",
            "account_number_masked": "****1234",
            "account_type": "checking",
            "kind": "bank_statement",
            "currency": "USD",
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
            "billing_cycle_length_days": 31,
            "starting_balance_cents": 10000,
            "ending_balance_cents": 12500,
            "transactions_count": 1,
            "net_credits_cents": 3000,
            "net_debits_cents": 500,
            "credit_limit_cents": None,
            "available_credit_cents": None,
            "previous_balance_cents": None,
            "new_balance_cents": None,
            "payment_due_amount_cents": None,
            "payment_due_date": None,
            "pages": 2,
        },
        "transactions": [
            {
                "row": 1,
                "source_file": "statement.pdf",
                "page": 1,
                "line_index": 2,
                "date": "2026-07-02",
                "description": "Deposit",
                "amount_cents": 3000,
                "transaction_type": "credit",
                "balance_after_cents": 13000,
                "currency": "USD",
                "validation_status": "valid",
                "warning_flags": [],
                "cardholder": "",
                "cardholder_card_masked": "",
            }
        ],
        "has_warnings": False,
    }


def local_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeAPIClient:
    def __init__(
        self,
        *,
        state: str = "succeeded",
        timed_out: bool = False,
        result: object | None = None,
        balance: dict[str, int] | None = None,
        page: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.state = state
        self.timed_out = timed_out
        self.result = conversion_data() if result is None else result
        self.balance = balance or {"balance": 20, "reserved": 3, "available": 17}
        self.page = page or {
            "results": [job("processing")],
            "next": "https://api.mainbook.ai/api/v1/developer/jobs?cursor=next%2Bcursor&page_size=25",
            "previous": None,
        }
        self.error = error
        self.calls: list[tuple[str, object]] = []

    async def create_job(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create_job", kwargs))
        return {
            "job_id": JOB_ID,
            "upload": {
                "url": "https://storage.example/upload",
                "method": "PUT",
                "headers": {"Content-Type": "application/pdf"},
                "expires_at": "2026-08-10T12:15:00Z",
            },
            "credits_reserved": 2,
        }

    async def upload_pdf(self, upload: object, data: bytes) -> None:
        self.calls.append(("upload_pdf", (upload, data)))

    async def start_job(self, job_id: str) -> dict[str, object]:
        self.calls.append(("start_job", job_id))
        return job("queued")

    async def poll_job(self, job_id: str, *, timeout_seconds: int, on_progress=None) -> PollOutcome:
        self.calls.append(("poll_job", (job_id, timeout_seconds)))
        return PollOutcome(job=job(self.state), timed_out=self.timed_out)

    async def get_job(self, job_id: str) -> dict[str, object]:
        self.calls.append(("get_job", job_id))
        return job(self.state)

    async def get_result(self, job_id: str, result_type: str) -> object:
        self.calls.append(("get_result", (job_id, result_type)))
        return self.result

    async def get_balance(self) -> dict[str, int]:
        self.calls.append(("get_balance", None))
        if self.error:
            raise self.error
        return self.balance

    async def list_jobs(self, *, limit: int, cursor: str | None) -> dict[str, object]:
        self.calls.append(("list_jobs", (limit, cursor)))
        return self.page


def server_with_fake(fake: FakeAPIClient, captured_keys: list[str] | None = None):
    keys = captured_keys if captured_keys is not None else []

    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        keys.append(api_key)
        assert base_url == "https://api.mainbook.ai"
        yield fake

    async def source_loader(request) -> PDFSource:
        assert request.file_path == "/tmp/statement.pdf"
        return PDFSource(filename="statement.pdf", data=b"%PDF-safe", size_bytes=9, page_count=2)

    return create_server(
        transport="stdio",
        client_factory=client_factory,
        source_loader=source_loader,
    )


@pytest.mark.asyncio
async def test_tools_expose_exact_names_annotations_and_nonopaque_schemas() -> None:
    """D8 task spec §§3.1-3.4 and §7: list_tools is the source of annotation/schema truth."""
    server = server_with_fake(FakeAPIClient())

    async with Client(server) as client:
        listed = await client.list_tools()

    tools = {tool.name: tool for tool in listed.tools}
    assert list(tools) == [
        "convert_bank_statement",
        "get_balance",
        "list_conversions",
        "get_conversion",
        "output_folder",
    ]
    convert = tools["convert_bank_statement"]
    assert convert.annotations is not None
    assert convert.annotations.read_only_hint is False
    assert convert.annotations.destructive_hint is False
    assert convert.annotations.idempotent_hint is False
    assert convert.annotations.open_world_hint is True
    assert convert.input_schema["properties"]["timeout_seconds"]["minimum"] == 30
    assert convert.input_schema["properties"]["timeout_seconds"]["maximum"] == 900
    assert convert.input_schema["properties"]["idempotency_key"]["anyOf"][0]["maxLength"] == 255
    assert (
        "only available over stdio"
        in convert.input_schema["properties"]["file_path"]["description"].lower()
    )
    file_path_description = convert.input_schema["properties"]["file_path"]["description"].lower()
    assert "allowed folders" in file_path_description
    assert all(name in file_path_description for name in ("downloads", "desktop", "documents"))
    assert "ConversionDocument" in json.dumps(convert.output_schema)
    assert "ConversionTransaction" in json.dumps(convert.output_schema)

    for name in ("get_balance", "list_conversions"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.open_world_hint is True
        assert annotations.destructive_hint is None
        assert annotations.idempotent_hint is None

    get_conversion = tools["get_conversion"].annotations
    assert get_conversion is not None
    assert get_conversion.read_only_hint is False
    assert get_conversion.destructive_hint is False
    assert get_conversion.idempotent_hint is False
    assert get_conversion.open_world_hint is True

    output_folder = tools["output_folder"].annotations
    assert output_folder is not None
    assert output_folder.read_only_hint is False
    assert output_folder.destructive_hint is False
    assert output_folder.idempotent_hint is True


@pytest.mark.asyncio
async def test_stdio_file_path_reaches_source_loader(monkeypatch) -> None:
    """Review A: local stdio keeps file_path support."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient()
    loaded: list[object] = []

    async def source_loader(request) -> PDFSource:
        loaded.append(request)
        return PDFSource(filename="statement.pdf", data=b"%PDF-safe", size_bytes=9, page_count=2)

    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        yield fake

    server = create_server(
        transport="stdio",
        client_factory=client_factory,
        source_loader=source_loader,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/tmp/statement.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is not True
    assert len(loaded) == 1
    assert loaded[0].file_path == "/tmp/statement.pdf"


@pytest.mark.asyncio
async def test_create_server_rejects_local_file_outside_its_allowed_roots(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(local_pdf_bytes())
    fake = FakeAPIClient()

    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        yield fake

    server = create_server(
        transport="stdio",
        allowed_roots=(allowed,),
        client_factory=client_factory,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": str(outside), "timeout_seconds": 30},
        )

    assert result.is_error is True
    assert "allowed folders" in result.content[0].text.lower()
    assert str(allowed.resolve()) in result.content[0].text
    assert str(outside) not in result.content[0].text
    assert fake.calls == []


@pytest.mark.asyncio
async def test_create_server_accepts_local_file_inside_its_allowed_roots(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    allowed = tmp_path / "allowed"
    nested = allowed / "nested"
    nested.mkdir(parents=True)
    path = nested / "statement.pdf"
    path.write_bytes(local_pdf_bytes())
    fake = FakeAPIClient()

    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        yield fake

    server = create_server(
        transport="stdio",
        allowed_roots=(allowed,),
        client_factory=client_factory,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": str(path), "timeout_seconds": 30},
        )

    assert result.is_error is not True
    create_call = next(value for name, value in fake.calls if name == "create_job")
    assert create_call["filename"] == "statement.pdf"
    assert create_call["page_count"] == 1


@pytest.mark.asyncio
async def test_create_server_with_no_active_roots_starts_and_rejects_local_files(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    path = tmp_path / "statement.pdf"
    path.write_bytes(local_pdf_bytes())
    fake = FakeAPIClient()

    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        yield fake

    server = create_server(
        transport="stdio",
        allowed_roots=(),
        client_factory=client_factory,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": str(path), "timeout_seconds": 30},
        )

    assert result.is_error is True
    assert "none active" in result.content[0].text.lower()
    assert fake.calls == []


@pytest.mark.asyncio
async def test_http_file_path_is_rejected_before_source_loader(monkeypatch) -> None:
    """Review A: remote clients cannot use the MCP host as a filesystem oracle."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient()
    loaded: list[object] = []

    async def source_loader(request) -> PDFSource:
        loaded.append(request)
        raise AssertionError("HTTP file_path reached the source loader")

    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        yield fake

    server = create_server(
        transport="http",
        client_factory=client_factory,
        source_loader=source_loader,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/private/secret.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is True
    assert "local paths are only available over stdio" in result.content[0].text.lower()
    assert "file_url" in result.content[0].text
    assert "/private/secret.pdf" not in result.content[0].text
    assert loaded == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_http_file_url_still_reaches_source_loader(monkeypatch) -> None:
    """Review A: remote mode keeps the public-URL conversion path."""
    monkeypatch.setattr(
        server_module,
        "_api_key_for_request",
        lambda ctx, *, transport: API_KEY,
    )
    fake = FakeAPIClient()
    loaded: list[object] = []

    async def source_loader(request) -> PDFSource:
        loaded.append(request)
        return PDFSource(filename="statement.pdf", data=b"%PDF-safe", size_bytes=9, page_count=2)

    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        yield fake

    server = create_server(
        transport="http",
        client_factory=client_factory,
        source_loader=source_loader,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_url": "https://documents.example/statement.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is not True
    assert len(loaded) == 1
    assert loaded[0].file_url == "https://documents.example/statement.pdf"


@pytest.mark.asyncio
async def test_convert_json_runs_full_cycle_and_returns_structured_export(monkeypatch) -> None:
    """D8 task spec §3.1: create -> verbatim upload -> start -> poll -> inline JSON."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient()
    keys: list[str] = []
    server = server_with_fake(fake, keys)

    async with Client(server) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/tmp/statement.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["job_id"] == JOB_ID
    assert result.structured_content["state"] == "succeeded"
    assert result.structured_content["validation"] == {
        "reconcilable": True,
        "passed": True,
        "mismatched_rows": 0,
    }
    assert result.structured_content["data"] == conversion_data()
    assert [name for name, _ in fake.calls] == [
        "create_job",
        "upload_pdf",
        "start_job",
        "poll_job",
        "get_result",
    ]
    assert keys == [API_KEY]


@pytest.mark.asyncio
async def test_convert_timeout_returns_resumable_job_instead_of_error(monkeypatch) -> None:
    """D8 task spec §3.1: poll timeout tells the agent to use get_conversion."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient(state="processing", timed_out=True)

    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/tmp/statement.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["timed_out"] is True
    assert result.structured_content["job_id"] == JOB_ID
    assert "get_conversion" in result.structured_content["message"]
    assert all(name != "get_result" for name, _ in fake.calls)


@pytest.mark.asyncio
async def test_convert_timeout_before_first_snapshot_reports_unknown_state(monkeypatch) -> None:
    """Review C: MCP output must distinguish no REST snapshot from confirmed processing."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)

    class NoSnapshotAPI(FakeAPIClient):
        async def poll_job(
            self, job_id: str, *, timeout_seconds: int, on_progress=None
        ) -> PollOutcome:
            self.calls.append(("poll_job", (job_id, timeout_seconds)))
            return PollOutcome(job=None, timed_out=True)

    fake = NoSnapshotAPI()
    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/tmp/statement.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["job_id"] == JOB_ID
    assert result.structured_content["state"] is None
    assert result.structured_content["pages"] is None
    assert result.structured_content["timed_out"] is True
    assert "before mainbook returned a job status" in result.structured_content["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_type", ["xlsx", "csv"])
async def test_convert_binary_type_never_places_bytes_in_mcp_response(
    monkeypatch, tmp_path, result_type: str
) -> None:
    """XLSX/CSV bytes go to an allowlisted file, never into model context."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "statement.pdf"
    source.write_bytes(local_pdf_bytes())
    binary = b"binary must be fetched but never embedded"
    fake = FakeAPIClient(result=binary)

    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        yield fake

    server = create_server(
        transport="stdio",
        allowed_roots=(allowed,),
        client_factory=client_factory,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": str(source), "result_type": result_type, "timeout_seconds": 30},
        )

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["data"] is None
    assert result.structured_content["download"] is None
    expected = allowed / f"statement.{result_type}"
    assert expected.read_bytes() == binary
    assert result.structured_content["saved_file"]["path"] == str(expected.resolve())
    assert "binary must be fetched" not in json.dumps(result.structured_content)
    assert ("get_result", (JOB_ID, result_type)) in fake.calls


@pytest.mark.asyncio
async def test_get_balance_explains_page_units(monkeypatch) -> None:
    """D8 task spec §3.2: balance output states that all three values are pages."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient()

    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool("get_balance", {})

    assert result.structured_content == {
        "balance": 20,
        "reserved": 3,
        "available": 17,
        "units": "pages",
        "explanation": "All values are page credits; one page credit converts one PDF page.",
    }


@pytest.mark.asyncio
async def test_list_conversions_returns_only_next_cursor(monkeypatch) -> None:
    """D8 task spec §3.3: one cursor page returns an opaque next_cursor for the next call."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient()

    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool("list_conversions", {"limit": 25, "cursor": "current"})

    assert result.structured_content is not None
    assert result.structured_content["count"] == 1
    assert result.structured_content["next_cursor"] == "next+cursor"
    assert result.structured_content["conversions"][0]["state"] == "processing"
    assert fake.calls == [("list_jobs", (25, "current"))]


@pytest.mark.asyncio
async def test_get_conversion_returns_status_and_json_when_ready(monkeypatch) -> None:
    """D8 task spec §3.4: a timed-out job remains reachable by job_id and returns its result."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient()

    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool("get_conversion", {"job_id": JOB_ID})

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["state"] == "succeeded"
    assert result.structured_content["data"] == conversion_data()
    assert [name for name, _ in fake.calls] == ["get_job", "get_result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failed", "insufficient_credits", "expired"])
async def test_actionable_job_states_are_errors_without_internal_reasons(
    monkeypatch, state: str
) -> None:
    """D8 task spec §§5-6: failed/credit/expired states tell the agent what to do safely."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient(state=state)

    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool("get_conversion", {"job_id": JOB_ID})

    assert result.is_error is True
    text = result.content[0].text
    assert JOB_ID in text
    assert "internal_category_must_not_escape" not in text
    assert "internal_reason_must_not_escape" not in text
    assert API_KEY not in text


@pytest.mark.asyncio
async def test_missing_key_is_an_mcp_error_before_client_creation(monkeypatch) -> None:
    """Stdio without an environment or stored credential points to auth login."""
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(server_module, "load_credential", lambda api_base: None)
    keys: list[str] = []
    server = server_with_fake(FakeAPIClient(), keys)

    async with Client(server) as client:
        result = await client.call_tool("get_balance", {})

    assert result.is_error is True
    assert "mainbook-mcp auth login" in result.content[0].text
    assert keys == []


@pytest.mark.asyncio
async def test_key_material_never_reaches_logs_or_error_messages(monkeypatch, caplog) -> None:
    """D8 task spec §§4 and 7: revoked keys never appear in logs or MCP error content."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient(
        error=MainBookAPIError(
            "The MainBook API key is invalid or revoked. Check MAINBOOK_API_KEY.",
            status_code=401,
            reason="invalid_key",
        )
    )
    caplog.set_level(logging.DEBUG)

    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool("get_balance", {})

    assert result.is_error is True
    assert API_KEY not in result.content[0].text
    assert API_KEY not in "\n".join(record.getMessage() for record in caplog.records)


def counting_loader(counter: list[int]):
    """Source loader that records every attempt to touch a file or the network."""

    async def loader(request) -> PDFSource:
        counter.append(1)
        return PDFSource(filename="statement.pdf", data=b"%PDF-safe", size_bytes=9, page_count=2)

    return loader


def http_server(fake: FakeAPIClient, counter: list[int]):
    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[FakeAPIClient]:
        yield fake

    return create_server(
        transport="http",
        client_factory=client_factory,
        source_loader=counting_loader(counter),
    )


@pytest.mark.asyncio
async def test_http_without_any_key_never_reaches_the_source_loader(monkeypatch) -> None:
    """Review D: an anonymous remote caller must not make the server fetch anything.

    Spec §8.5 keys every developer limit on the *authenticated* key, so an
    unauthenticated request that still triggers an outbound download would be
    unmetered bandwidth spent on our address.
    """
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    fake = FakeAPIClient()
    attempts: list[int] = []

    async with Client(http_server(fake, attempts)) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_url": "https://files.example/statement.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is True
    assert attempts == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_http_rejects_an_unusable_key_before_downloading(monkeypatch) -> None:
    """HTTP ignores an environment key instead of treating server-local state as caller auth."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient()
    attempts: list[int] = []

    async with Client(http_server(fake, attempts)) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_url": "https://files.example/statement.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is True
    assert attempts == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_stdio_environment_wins_without_reading_storage(monkeypatch) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setattr(
        server_module,
        "load_credential",
        lambda api_base: (_ for _ in ()).throw(AssertionError("storage was read")),
    )
    captured: list[str] = []

    async with Client(server_with_fake(FakeAPIClient(), captured)) as client:
        result = await client.call_tool("get_balance", {})

    assert result.is_error is not True
    assert captured == [API_KEY]


@pytest.mark.asyncio
async def test_stdio_uses_stored_credential_after_environment(monkeypatch) -> None:
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    stored_key = "mb_live_stored_stdio_key"
    bases: list[str] = []

    def load_credential(api_base: str) -> StoredCredential:
        bases.append(api_base)
        return StoredCredential(
            api_base=api_base,
            api_key=stored_key,
            client_name="MainBook MCP on test-host",
            account=None,
            created_at=None,
            storage="file",
        )

    monkeypatch.setattr(server_module, "load_credential", load_credential)
    captured: list[str] = []

    async with Client(server_with_fake(FakeAPIClient(), captured)) as client:
        result = await client.call_tool("get_balance", {})

    assert result.is_error is not True
    assert captured == [stored_key]
    assert bases == ["https://mainbook.ai"]


@pytest.mark.asyncio
async def test_stdio_conversion_adds_no_verification_request(monkeypatch) -> None:
    """Review D: the stdio caller is the key owner, so the extra round trip is waste."""
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    fake = FakeAPIClient()

    async with Client(server_with_fake(fake)) as client:
        result = await client.call_tool(
            "convert_bank_statement",
            {"file_path": "/tmp/statement.pdf", "timeout_seconds": 30},
        )

    assert result.is_error is not True
    assert "get_balance" not in [name for name, _ in fake.calls]
