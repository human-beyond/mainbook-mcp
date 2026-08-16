"""Behavioral tests for local result delivery and output-folder preferences."""

from __future__ import annotations

import io
import json
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from pypdf import PdfWriter

from mainbook_mcp.client import PollOutcome
from mainbook_mcp.errors import MainBookFileError
from mainbook_mcp.files import PDFSource
from mainbook_mcp.server import create_server

API_KEY = "mb_live_test_output_files"
JOB_ID = "00000000-0000-0000-0000-000000000042"
BINARY_RESULT = b"REST-export-bytes\x00must-match-exactly"


def _job(state: str = "succeeded") -> dict[str, Any]:
    return {
        "job_id": JOB_ID,
        "state": state,
        "filename": "statement.pdf",
        "file_format": "pdf",
        "pages": 1,
        "credits_reserved": None,
        "source": "api",
        "created_at": "2026-08-11T12:00:00Z",
        "updated_at": "2026-08-11T12:00:10Z",
        "validation": {"reconcilable": True, "passed": True, "mismatched_rows": 0},
        "error": None,
    }


def _conversion_data() -> dict[str, Any]:
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
            "pages": 1,
        },
        "transactions": [
            {
                "row": 1,
                "source_file": "statement.pdf",
                "page": 1,
                "line_index": 0,
                "date": "2026-07-02",
                "description": "Deposit",
                "amount_cents": 2500,
                "transaction_type": "credit",
                "balance_after_cents": 12500,
                "currency": "USD",
                "validation_status": "valid",
                "warning_flags": [],
                "cardholder": "",
                "cardholder_card_masked": "",
            }
        ],
        "has_warnings": False,
    }


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class OutputAPI:
    def __init__(self, *, binary: bytes = BINARY_RESULT) -> None:
        self.binary = binary
        self.calls: list[tuple[str, object]] = []

    async def get_balance(self) -> dict[str, int]:
        self.calls.append(("get_balance", None))
        return {"balance": 20, "reserved": 0, "available": 20}

    async def create_job(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create_job", kwargs))
        return {
            "job_id": JOB_ID,
            "upload": {"url": "https://storage.test/upload", "method": "PUT", "headers": {}},
        }

    async def upload_pdf(self, upload: object, data: bytes) -> None:
        self.calls.append(("upload_pdf", data))

    async def start_job(self, job_id: str) -> dict[str, object]:
        self.calls.append(("start_job", job_id))
        return _job("queued")

    async def poll_job(
        self, job_id: str, *, timeout_seconds: int, on_progress=None
    ) -> PollOutcome:
        self.calls.append(("poll_job", (job_id, timeout_seconds)))
        return PollOutcome(job=_job(), timed_out=False)

    async def get_job(self, job_id: str) -> dict[str, object]:
        self.calls.append(("get_job", job_id))
        return _job()

    async def get_result(self, job_id: str, result_type: str) -> object:
        self.calls.append(("get_result", (job_id, result_type)))
        return _conversion_data() if result_type == "json" else self.binary


def _factory(api: OutputAPI):
    @asynccontextmanager
    async def client_factory(api_key: str, base_url: str) -> AsyncIterator[OutputAPI]:
        assert api_key == API_KEY
        yield api

    return client_factory


def _write_preferences(home: Path, output_folder: str, *, valid_json: bool = True) -> Path:
    preferences = home / ".mainbook" / "preferences.json"
    preferences.parent.mkdir(parents=True)
    if valid_json:
        preferences.write_text(json.dumps({"output_folder": output_folder}), encoding="utf-8")
    else:
        preferences.write_text("{not valid JSON", encoding="utf-8")
    return preferences


async def _convert(
    *,
    api: OutputAPI,
    source: Path,
    allowed_roots: tuple[Path, ...],
    result_type: str = "xlsx",
    output_path: str | None = None,
):
    arguments = {
        "file_path": str(source),
        "result_type": result_type,
        "timeout_seconds": 30,
    }
    if output_path is not None:
        arguments["output_path"] = output_path
    server = create_server(
        transport="stdio",
        allowed_roots=allowed_roots,
        client_factory=_factory(api),
    )
    async with Client(server) as client:
        return await client.call_tool("convert_bank_statement", arguments)


@pytest.mark.asyncio
async def test_binary_defaults_next_to_source_with_full_path_reason_and_exact_rest_bytes(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "statement.pdf"
    source.write_bytes(_pdf_bytes())

    result = await _convert(api=OutputAPI(), source=source, allowed_roots=(allowed,))

    expected = allowed / "statement.xlsx"
    assert result.is_error is not True
    assert expected.read_bytes() == BINARY_RESULT
    assert result.structured_content["saved_file"] == {
        "path": str(expected.resolve()),
        "reason": "placed next to the source PDF",
    }
    assert str(expected.resolve()) in result.structured_content["message"]
    assert "next to the source PDF" in result.structured_content["message"]


@pytest.mark.asyncio
async def test_existing_result_is_never_overwritten(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "statement.pdf"
    source.write_bytes(_pdf_bytes())
    original = allowed / "statement.xlsx"
    original.write_bytes(b"user-edited workbook")

    result = await _convert(api=OutputAPI(), source=source, allowed_roots=(allowed,))

    collision_safe = allowed / "statement (2).xlsx"
    assert result.is_error is not True
    assert original.read_bytes() == b"user-edited workbook"
    assert collision_safe.read_bytes() == BINARY_RESULT
    assert result.structured_content["saved_file"]["path"] == str(collision_safe.resolve())


@pytest.mark.asyncio
async def test_saved_output_folder_is_used_and_reported(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    allowed = tmp_path / "allowed"
    source_folder = allowed / "source"
    destination = allowed / "exports"
    source_folder.mkdir(parents=True)
    destination.mkdir()
    source = source_folder / "statement.pdf"
    source.write_bytes(_pdf_bytes())
    _write_preferences(home, str(destination.resolve()))

    result = await _convert(api=OutputAPI(), source=source, allowed_roots=(allowed,))

    expected = destination / "statement.xlsx"
    assert expected.read_bytes() == BINARY_RESULT
    assert result.structured_content["saved_file"] == {
        "path": str(expected.resolve()),
        "reason": "placed in the configured default output folder",
    }


@pytest.mark.asyncio
async def test_call_output_path_overrides_saved_folder_and_forces_extension(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    allowed = tmp_path / "allowed"
    source_folder = allowed / "source"
    configured = allowed / "configured"
    explicit = allowed / "explicit"
    for folder in (source_folder, configured, explicit):
        folder.mkdir(parents=True, exist_ok=True)
    source = source_folder / "statement.pdf"
    source.write_bytes(_pdf_bytes())
    _write_preferences(home, str(configured.resolve()))

    requested = explicit / "my-export.wrong"
    result = await _convert(
        api=OutputAPI(),
        source=source,
        allowed_roots=(allowed,),
        output_path=str(requested),
    )

    expected = explicit / "my-export.xlsx"
    assert expected.read_bytes() == BINARY_RESULT
    assert not (configured / "statement.xlsx").exists()
    assert result.structured_content["saved_file"] == {
        "path": str(expected.resolve()),
        "reason": "placed at the output_path requested for this call",
    }


@pytest.mark.asyncio
async def test_output_path_outside_allowed_roots_is_rejected_without_creating_a_file(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    source = allowed / "statement.pdf"
    source.write_bytes(_pdf_bytes())
    api = OutputAPI()
    target = outside / "stolen.xlsx"

    result = await _convert(
        api=api,
        source=source,
        allowed_roots=(allowed,),
        output_path=str(target),
    )

    assert result.is_error is True
    assert "allowed folders" in result.content[0].text.lower()
    assert not target.exists()
    assert api.calls == []


@pytest.mark.asyncio
async def test_symlinked_parent_escaping_allowed_root_is_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    source = allowed / "statement.pdf"
    source.write_bytes(_pdf_bytes())
    escape = allowed / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    target = escape / "result.xlsx"

    result = await _convert(
        api=OutputAPI(),
        source=source,
        allowed_roots=(allowed,),
        output_path=str(target),
    )

    assert result.is_error is True
    assert "allowed folders" in result.content[0].text.lower()
    assert not (outside / "result.xlsx").exists()


@pytest.mark.asyncio
async def test_disallowed_saved_folder_is_ignored_with_visible_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    allowed = tmp_path / "allowed"
    stale = tmp_path / "formerly-allowed"
    allowed.mkdir()
    stale.mkdir()
    source = allowed / "statement.pdf"
    source.write_bytes(_pdf_bytes())
    _write_preferences(home, str(stale.resolve()))

    result = await _convert(api=OutputAPI(), source=source, allowed_roots=(allowed,))

    expected = allowed / "statement.xlsx"
    assert expected.read_bytes() == BINARY_RESULT
    assert "ignored" in result.structured_content["message"].lower()
    assert "no longer allowed" in result.structured_content["message"].lower()
    assert result.structured_content["saved_file"]["reason"] == "placed next to the source PDF"
    assert not (stale / "statement.xlsx").exists()


@pytest.mark.asyncio
async def test_corrupt_preferences_are_silently_treated_as_no_preference(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "statement.pdf"
    source.write_bytes(_pdf_bytes())
    _write_preferences(home, "unused", valid_json=False)

    result = await _convert(api=OutputAPI(), source=source, allowed_roots=(allowed,))

    expected = allowed / "statement.xlsx"
    assert result.is_error is not True
    assert expected.read_bytes() == BINARY_RESULT
    assert result.structured_content["saved_file"]["reason"] == "placed next to the source PDF"
    assert result.structured_content["message"].startswith("Conversion completed.")


@pytest.mark.asyncio
async def test_empty_allowed_roots_fail_closed_for_writes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    api = OutputAPI()
    target = tmp_path / "result.xlsx"
    server = create_server(transport="stdio", allowed_roots=(), client_factory=_factory(api))

    async with Client(server) as client:
        result = await client.call_tool(
            "get_conversion",
            {"job_id": JOB_ID, "result_type": "xlsx", "output_path": str(target)},
        )

    assert result.is_error is True
    assert "none active" in result.content[0].text.lower()
    assert not target.exists()
    assert api.calls == []


@pytest.mark.asyncio
async def test_json_stays_inline_and_explicit_output_path_also_writes_json(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "statement.pdf"
    source.write_bytes(_pdf_bytes())

    inline = await _convert(
        api=OutputAPI(), source=source, allowed_roots=(allowed,), result_type="json"
    )
    assert inline.is_error is not True
    assert inline.structured_content["data"] == _conversion_data()
    assert inline.structured_content.get("saved_file") is None
    assert not (allowed / "statement.json").exists()

    requested = allowed / "reviewed.txt"
    written = await _convert(
        api=OutputAPI(),
        source=source,
        allowed_roots=(allowed,),
        result_type="json",
        output_path=str(requested),
    )
    expected = allowed / "reviewed.json"
    assert written.is_error is not True
    assert written.structured_content["data"] == _conversion_data()
    assert json.loads(expected.read_text(encoding="utf-8")) == _conversion_data()
    assert written.structured_content["saved_file"]["path"] == str(expected.resolve())


@pytest.mark.asyncio
async def test_http_rejects_output_path_ignores_preferences_and_keeps_download_instruction(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    configured = tmp_path / "configured"
    configured.mkdir()
    _write_preferences(home, str(configured.resolve()))
    api = OutputAPI()
    loads: list[object] = []

    async def source_loader(request) -> PDFSource:
        loads.append(request)
        return PDFSource(filename="statement.pdf", data=b"%PDF-safe", size_bytes=9, page_count=1)

    server = create_server(
        transport="http",
        client_factory=_factory(api),
        source_loader=source_loader,
    )
    async with Client(server) as client:
        rejected = await client.call_tool(
            "convert_bank_statement",
            {
                "file_url": "https://files.example/statement.pdf",
                "result_type": "xlsx",
                "output_path": str(configured / "result.xlsx"),
                "timeout_seconds": 30,
            },
        )
        assert rejected.is_error is True
        assert "output_path" in rejected.content[0].text
        assert api.calls == []
        assert loads == []

        result = await client.call_tool(
            "convert_bank_statement",
            {
                "file_url": "https://files.example/statement.pdf",
                "result_type": "xlsx",
                "timeout_seconds": 30,
            },
        )

    assert result.is_error is not True
    assert result.structured_content["saved_file"] is None
    assert result.structured_content["download"]["result_type"] == "xlsx"
    assert "type=xlsx" in result.structured_content["download"]["rest_endpoint"]
    assert not (configured / "statement.xlsx").exists()
    assert all(name != "get_result" for name, _ in api.calls)


@pytest.mark.asyncio
async def test_get_conversion_without_destination_explains_what_is_missing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    api = OutputAPI()
    server = create_server(
        transport="stdio", allowed_roots=(allowed,), client_factory=_factory(api)
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "get_conversion", {"job_id": JOB_ID, "result_type": "xlsx"}
        )

    assert result.is_error is True
    text = result.content[0].text.lower()
    assert "do not know where" in text
    assert "output_path" in text
    assert "default output folder" in text
    assert list(allowed.iterdir()) == []
    assert api.calls == []


@pytest.mark.asyncio
async def test_output_folder_tool_reads_then_atomically_sets_and_resets_preference(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    allowed = tmp_path / "allowed"
    destination = allowed / "exports"
    destination.mkdir(parents=True)
    server = create_server(transport="stdio", allowed_roots=(allowed,))
    preferences = home / ".mainbook" / "preferences.json"

    async with Client(server) as client:
        initial = await client.call_tool("output_folder", {})
        assert initial.is_error is not True
        assert initial.structured_content["output_folder"] == "next_to_source"
        assert initial.structured_content["allowed_folders"] == [str(allowed.resolve())]
        assert not preferences.exists()

        changed = await client.call_tool("output_folder", {"path": str(destination)})
        assert changed.is_error is not True
        assert changed.structured_content["output_folder"] == str(destination.resolve())

        reread = await client.call_tool("output_folder", {})
        assert reread.structured_content["output_folder"] == str(destination.resolve())

        reset = await client.call_tool("output_folder", {"path": "next_to_source"})
        assert reset.structured_content["output_folder"] == "next_to_source"

    assert json.loads(preferences.read_text(encoding="utf-8")) == {
        "output_folder": "next_to_source"
    }
    assert stat.S_IMODE((home / ".mainbook").stat().st_mode) == 0o700
    assert stat.S_IMODE(preferences.stat().st_mode) == 0o600
    assert not list((home / ".mainbook").glob("*.tmp"))


@pytest.mark.asyncio
async def test_output_folder_tool_annotations_require_confirmation_and_are_idempotent(
    tmp_path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    server = create_server(transport="stdio", allowed_roots=(allowed,))

    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert list(tools) == [
        "convert_bank_statement",
        "get_balance",
        "list_conversions",
        "get_conversion",
        "output_folder",
    ]
    annotations = tools["output_folder"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True


@pytest.mark.asyncio
async def test_output_folder_rejects_disallowed_path_without_changing_preference(
    monkeypatch, tmp_path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    server = create_server(transport="stdio", allowed_roots=(allowed,))

    async with Client(server) as client:
        result = await client.call_tool("output_folder", {"path": str(outside)})

    assert result.is_error is True
    assert "allowed folders" in result.content[0].text.lower()
    assert not (home / ".mainbook" / "preferences.json").exists()


@pytest.mark.asyncio
async def test_http_output_folder_is_rejected_as_a_local_only_setting(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    server = create_server(transport="http", allowed_roots=(allowed,))

    async with Client(server) as client:
        result = await client.call_tool("output_folder", {"path": str(allowed)})

    assert result.is_error is True
    assert "stdio" in result.content[0].text.lower()


def test_result_write_refuses_a_folder_swapped_after_the_check(tmp_path) -> None:
    """The allowed folder is checked, then replaced by a symlink before the write.

    Without a pinned descriptor the result file followed the new symlink and landed outside
    the allowlist, which is exactly what SECURITY.md promises cannot happen.
    """
    import os

    from mainbook_mcp.output import prepare_output_path, write_result_bytes

    allowed = tmp_path / "allowed"
    inside = allowed / "sub"
    inside.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    roots = (allowed.resolve(),)
    target = prepare_output_path(
        str(inside), result_type="xlsx", default_filename="statement.xlsx", allowed_roots=roots
    )

    inside.rmdir()
    os.symlink(outside, inside)

    with pytest.raises(MainBookFileError):
        write_result_bytes(target, b"payload", roots)

    assert not (outside / "statement.xlsx").exists()


def test_result_write_still_works_in_an_untouched_allowed_folder(tmp_path) -> None:
    from mainbook_mcp.output import prepare_output_path, write_result_bytes

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    roots = (allowed.resolve(),)
    target = prepare_output_path(
        str(allowed), result_type="csv", default_filename="statement.csv", allowed_roots=roots
    )

    first = write_result_bytes(target, b"a,b\n", roots)
    second = write_result_bytes(target, b"a,b\n", roots)

    assert first.read_bytes() == b"a,b\n"
    assert first.parent == allowed.resolve()
    assert second.name == "statement (2).csv"
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
