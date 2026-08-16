"""PDF and SSRF tests derived from D8 task spec §§3.1, 4 and 6."""

from __future__ import annotations

import io
import os
import re
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx2 as httpx
import pytest
from pypdf import PdfWriter

from mainbook_mcp.errors import MainBookFileError
from mainbook_mcp.files import (
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    download_pdf_url,
    load_local_pdf,
    normalize_allowed_roots,
)


def pdf_bytes(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def valid_prefixed_pdf_bytes(prefix: bytes) -> bytes:
    """Add a legal pre-header prefix and keep every PDF byte offset valid."""
    data = pdf_bytes()
    shift = len(prefix)
    adjusted = re.sub(
        rb"(?m)^(\d{10}) 00000 n",
        lambda match: f"{int(match.group(1)) + shift:010d} 00000 n".encode(),
        data,
    )
    adjusted = re.sub(
        rb"startxref\n(\d+)",
        lambda match: b"startxref\n" + str(int(match.group(1)) + shift).encode(),
        adjusted,
    )
    return prefix + adjusted


def public_resolver(*addresses: str):
    async def resolve(host: str, port: int) -> Sequence[str]:
        assert host
        assert port == 443
        return addresses or ("93.184.216.34",)

    return resolve


def filesystem_is_case_insensitive(directory: Path) -> bool:
    """Probe the test filesystem instead of assuming semantics from the OS name."""
    probe = directory / "MainBookCaseProbe"
    probe.write_text("probe")
    alias = directory / "mainbookcaseprobe"
    return alias.exists() and alias.samefile(probe)


def test_public_limits_are_exact() -> None:
    """Design spec §5.2: public inputs stop at 50 MiB and 500 PDF pages."""
    assert MAX_PDF_BYTES == 50 * 1024 * 1024
    assert MAX_PDF_PAGES == 500


@pytest.mark.asyncio
async def test_local_pdf_metadata_is_computed_by_server(tmp_path) -> None:
    """D8 task spec §3.1: MCP computes byte size and page count instead of trusting the agent."""
    path = tmp_path / "two pages.pdf"
    path.write_bytes(pdf_bytes(2))

    source = await load_local_pdf(str(path), allowed_roots=(tmp_path,))

    assert source.filename == "two pages.pdf"
    assert source.size_bytes == len(source.data)
    assert source.page_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "directory", "corrupt"])
async def test_local_pdf_failure_is_human_readable(tmp_path, kind: str) -> None:
    """D8 task spec §6: missing or broken local input must not expose a traceback."""
    path = tmp_path / "statement.pdf"
    if kind == "directory":
        path.mkdir()
    elif kind == "corrupt":
        path.write_bytes(b"this is not a pdf")

    with pytest.raises(MainBookFileError) as raised:
        await load_local_pdf(str(path), allowed_roots=(tmp_path,))

    assert "Traceback" not in str(raised.value)
    assert any(
        word in str(raised.value).lower()
        for word in ("not found", "regular file", "not a pdf")
    )


@pytest.mark.asyncio
async def test_local_pdf_larger_than_limit_is_rejected_before_read(tmp_path) -> None:
    """D8 task spec §4: local PDFs are bounded by bytes before they are loaded into memory."""
    path = tmp_path / "large.pdf"
    path.write_bytes(pdf_bytes() + b"padding")

    with pytest.raises(MainBookFileError, match="larger than"):
        await load_local_pdf(
            str(path),
            allowed_roots=(tmp_path,),
            max_bytes=len(pdf_bytes()),
        )


@pytest.mark.asyncio
async def test_pdf_with_501_pages_is_rejected(tmp_path) -> None:
    """Design spec §5.2 and D8 task §6: page 501 is outside the public contract."""
    path = tmp_path / "501-pages.pdf"
    path.write_bytes(pdf_bytes(MAX_PDF_PAGES + 1))

    with pytest.raises(MainBookFileError, match="more than 500 pages"):
        await load_local_pdf(str(path), allowed_roots=(tmp_path,))


@pytest.mark.asyncio
async def test_local_pdf_in_nested_allowed_directory_is_accepted(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    nested = allowed / "statements" / "2026"
    nested.mkdir(parents=True)
    path = nested / "statement.pdf.download"
    path.write_bytes(pdf_bytes())

    source = await load_local_pdf(str(path), allowed_roots=(allowed,))

    assert source.filename == "statement.pdf.download"
    assert source.page_count == 1


@pytest.mark.asyncio
async def test_local_pdf_with_different_path_case_is_accepted_on_case_insensitive_filesystem(
    tmp_path,
) -> None:
    if not filesystem_is_case_insensitive(tmp_path):
        pytest.skip("requires a case-insensitive filesystem")

    allowed = tmp_path / "Allowed"
    allowed.mkdir()
    path = allowed / "Statement.pdf"
    path.write_bytes(pdf_bytes())
    differently_cased_path = tmp_path / "allowed" / "statement.pdf"

    source = await load_local_pdf(str(differently_cased_path), allowed_roots=(allowed,))

    assert source.filename.lower() == "statement.pdf"
    assert source.page_count == 1


@pytest.mark.asyncio
async def test_case_different_outside_directory_is_rejected_on_case_sensitive_filesystem(
    tmp_path,
) -> None:
    if filesystem_is_case_insensitive(tmp_path):
        pytest.skip("distinct case-only directory names require a case-sensitive filesystem")

    allowed = tmp_path / "Allowed"
    allowed.mkdir()
    outside = tmp_path / "allowed"
    outside.mkdir()
    path = outside / "statement.pdf"
    path.write_bytes(pdf_bytes())

    with pytest.raises(MainBookFileError, match="allowed folders"):
        await load_local_pdf(str(path), allowed_roots=(allowed,))


@pytest.mark.asyncio
async def test_local_pdf_outside_allowed_directories_is_rejected(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    path = tmp_path / "outside.pdf"
    path.write_bytes(pdf_bytes())

    with pytest.raises(MainBookFileError) as raised:
        await load_local_pdf(str(path), allowed_roots=(allowed,))

    assert str(allowed.resolve()) in str(raised.value)
    assert "allowed" in str(raised.value).lower()


@pytest.mark.asyncio
async def test_symlink_inside_allowed_directory_to_outside_file_is_rejected(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf_bytes())
    link = allowed / "statement.pdf"
    link.symlink_to(outside)

    with pytest.raises(MainBookFileError) as raised:
        await load_local_pdf(str(link), allowed_roots=(allowed,))

    assert str(allowed.resolve()) in str(raised.value)
    assert str(outside) not in str(raised.value)


@pytest.mark.asyncio
async def test_dot_dot_path_escaping_allowed_directory_is_rejected(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    nested = allowed / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf_bytes())
    escaping_path = nested / ".." / ".." / outside.name

    with pytest.raises(MainBookFileError) as raised:
        await load_local_pdf(str(escaping_path), allowed_roots=(allowed,))

    assert str(allowed.resolve()) in str(raised.value)


@pytest.mark.asyncio
async def test_empty_allowed_roots_reject_every_local_file(tmp_path) -> None:
    path = tmp_path / "statement.pdf"
    path.write_bytes(pdf_bytes())

    with pytest.raises(MainBookFileError) as raised:
        await load_local_pdf(str(path), allowed_roots=())

    assert "none" in str(raised.value).lower()


@pytest.mark.asyncio
async def test_allowed_root_itself_is_not_a_valid_file_path(tmp_path) -> None:
    with pytest.raises(MainBookFileError, match="allowed folders"):
        await load_local_pdf(str(tmp_path), allowed_roots=(tmp_path,))


@pytest.mark.asyncio
async def test_pdf_extension_without_magic_bytes_is_rejected_before_pypdf(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "statement.pdf"
    path.write_bytes(b"plain text disguised as a PDF")

    def unexpected_reader(*args, **kwargs):
        raise AssertionError("pypdf must not inspect a file without PDF magic bytes")

    monkeypatch.setattr("mainbook_mcp.files.PdfReader", unexpected_reader)

    with pytest.raises(MainBookFileError, match="not a PDF"):
        await load_local_pdf(str(path), allowed_roots=(tmp_path,))


@pytest.mark.asyncio
async def test_pdf_magic_bytes_within_first_1024_bytes_are_accepted(tmp_path) -> None:
    prefix = b"ignored transport prefix\n"
    path = tmp_path / "statement.download"
    path.write_bytes(valid_prefixed_pdf_bytes(prefix))

    source = await load_local_pdf(str(path), allowed_roots=(tmp_path,))

    assert source.data.startswith(prefix)
    assert source.page_count == 1


@pytest.mark.asyncio
async def test_oversized_local_file_uses_fstat_and_is_not_read(tmp_path, monkeypatch) -> None:
    path = tmp_path / "large.pdf"
    path.write_bytes(b"%PDF-" + b"x" * 20)

    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=0))

    def unexpected_read(self: Path) -> bytes:
        raise AssertionError("an oversized descriptor must not be read")

    def unexpected_fdopen(*args, **kwargs):
        raise AssertionError("an oversized descriptor must be rejected before reading")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    monkeypatch.setattr(os, "fdopen", unexpected_fdopen)

    with pytest.raises(MainBookFileError, match="larger than"):
        await load_local_pdf(str(path), allowed_roots=(tmp_path,), max_bytes=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["http://example.com/a.pdf", "ftp://example.com/a.pdf", "file:///tmp/a.pdf"]
)
async def test_file_url_allows_https_only(url: str) -> None:
    """D8 task spec §4 SSRF guard: file_url supports HTTPS and no other scheme."""
    with pytest.raises(MainBookFileError, match="HTTPS"):
        await download_pdf_url(url, resolver=public_resolver())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.10.20",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",  # noqa: S104 - literal is an SSRF rejection fixture, never a bind address
    ],
)
async def test_file_url_rejects_every_non_public_address_class(address: str) -> None:
    """D8 task spec §4 SSRF guard: private/loopback/link-local/metadata IPv4 and IPv6 are denied."""
    with pytest.raises(MainBookFileError, match="public Internet address"):
        await download_pdf_url(
            "https://documents.example/statement.pdf",
            resolver=public_resolver(address),
        )


@pytest.mark.asyncio
async def test_file_url_rejects_if_any_dns_answer_is_private() -> None:
    """D8 task spec §4 SSRF guard: mixed DNS answers fail closed instead of choosing the public one."""
    with pytest.raises(MainBookFileError, match="public Internet address"):
        await download_pdf_url(
            "https://documents.example/statement.pdf",
            resolver=public_resolver("93.184.216.34", "127.0.0.1"),
        )


@pytest.mark.asyncio
async def test_file_url_does_not_follow_redirects() -> None:
    """D8 task spec §4 SSRF guard: redirects are not followed, including to metadata hosts."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/latest/meta-data"})

    with pytest.raises(MainBookFileError, match="redirect"):
        await download_pdf_url(
            "https://documents.example/statement.pdf",
            resolver=public_resolver(),
            transport=httpx.MockTransport(handler),
        )

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_file_url_rejects_oversized_content_length_before_streaming() -> None:
    """D8 task spec §4 SSRF guard: Content-Length is an early 50 MiB gate."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "11"}, content=b"ignored")

    with pytest.raises(MainBookFileError, match="Content-Length"):
        await download_pdf_url(
            "https://documents.example/statement.pdf",
            resolver=public_resolver(),
            transport=httpx.MockTransport(handler),
            max_bytes=10,
        )


@pytest.mark.asyncio
async def test_file_url_rejects_stream_that_exceeds_declared_or_missing_length() -> None:
    """D8 task spec §4 SSRF guard: the actual streamed byte count is independently bounded."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=b"12345678901")
        del response.headers["Content-Length"]
        return response

    with pytest.raises(MainBookFileError, match="download exceeded"):
        await download_pdf_url(
            "https://documents.example/statement.pdf",
            resolver=public_resolver(),
            transport=httpx.MockTransport(handler),
            max_bytes=10,
        )


@pytest.mark.asyncio
async def test_file_url_timeout_is_sanitized() -> None:
    """D8 task spec §4: remote download has a finite timeout and a safe error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("internal socket details", request=request)

    with pytest.raises(MainBookFileError, match="timed out") as raised:
        await download_pdf_url(
            "https://documents.example/statement.pdf",
            resolver=public_resolver(),
            transport=httpx.MockTransport(handler),
            timeout_seconds=1,
        )

    assert "internal socket" not in str(raised.value)


@pytest.mark.asyncio
async def test_dns_rebinding_cannot_replace_the_validated_destination() -> None:
    """Review B: a later private DNS answer cannot become the HTTP connection target."""
    body = pdf_bytes()
    resolve_calls = 0
    requested_hosts: list[str] = []

    async def changing_resolver(host: str, port: int) -> Sequence[str]:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 1:
            return ("93.184.216.34",)
        return ("127.0.0.1",)

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        return httpx.Response(200, content=body)

    source = await download_pdf_url(
        "https://documents.example/statement.pdf",
        resolver=changing_resolver,
        transport=httpx.MockTransport(handler),
    )

    assert source.page_count == 1
    assert resolve_calls == 1
    assert requested_hosts == ["93.184.216.34"]
    assert "127.0.0.1" not in requested_hosts


@pytest.mark.asyncio
async def test_remote_http_status_and_address_are_not_exposed() -> None:
    """Review B: an upstream response cannot turn the tool into a network scanner."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(451, content=b"private upstream detail")

    with pytest.raises(MainBookFileError) as raised:
        await download_pdf_url(
            "https://documents.example/private.pdf",
            resolver=public_resolver(),
            transport=httpx.MockTransport(handler),
        )

    message = str(raised.value)
    assert message == "The remote PDF link is unavailable."
    assert "451" not in message
    assert "documents.example" not in message


@pytest.mark.asyncio
async def test_valid_remote_pdf_returns_computed_metadata() -> None:
    """D8 task spec §3.1: remote mode downloads a public HTTPS PDF and computes its pages."""
    body = pdf_bytes(3)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://93.184.216.34/path/My%20Statement.pdf"
        assert request.headers["Host"] == "documents.example"
        assert request.extensions["sni_hostname"] == "documents.example"
        return httpx.Response(200, headers={"Content-Length": str(len(body))}, content=body)

    source = await download_pdf_url(
        "https://documents.example/path/My%20Statement.pdf",
        resolver=public_resolver(),
        transport=httpx.MockTransport(handler),
    )

    assert source.filename == "My Statement.pdf"
    assert source.size_bytes == len(body)
    assert source.page_count == 3


@pytest.mark.asyncio
async def test_unsubstituted_bundle_placeholders_resolve_to_real_folders(tmp_path) -> None:
    """Claude Desktop 1.26832.0 hands the server a literal "${HOME}/Downloads" argument.

    Observed on a live install: the allowlist came out empty and every local path was refused.
    """
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    statement = downloads / "statement.pdf"
    statement.write_bytes(pdf_bytes())

    with mock.patch.object(Path, "home", staticmethod(lambda: home)):
        roots = normalize_allowed_roots(["${HOME}/Downloads"])
        source = await load_local_pdf(str(statement), allowed_roots=roots)

    assert roots == (downloads.resolve(),)
    assert source.page_count == 1


def test_unknown_placeholder_is_not_expanded_into_an_allowed_root() -> None:
    """An unrecognised placeholder must stay unusable rather than collapse to something real."""
    assert normalize_allowed_roots(["${NOT_A_TOKEN}/Downloads"]) == ()
