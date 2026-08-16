"""Bounded PDF loading with an SSRF guard for remote file URLs."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import os
import socket
import stat
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx2 as httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .errors import MainBookFileError

MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 500
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 20.0

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


@dataclass(frozen=True)
class PDFSource:
    """Validated PDF bytes and the metadata required by POST /developer/jobs."""

    filename: str
    data: bytes
    size_bytes: int
    page_count: int
    local_path: Path | None = None


async def load_local_pdf(
    file_path: str,
    *,
    allowed_roots: Sequence[str | Path] = (),
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
) -> PDFSource:
    """Read a resolved, allowlisted regular file through one bounded descriptor."""
    try:
        path = await asyncio.to_thread(Path(file_path).expanduser().resolve, strict=True)
    except (OSError, RuntimeError) as exc:
        raise MainBookFileError("The local PDF was not found or cannot be read.") from exc

    roots = normalize_allowed_roots(allowed_roots)
    is_allowed = await asyncio.to_thread(_is_within_allowed_root, path, roots)
    if not is_allowed:
        displayed_roots = ", ".join(str(root) for root in roots) or "(none active)"
        raise MainBookFileError(
            "The local PDF must be inside one of the allowed folders: " + displayed_roots
        )

    data = await asyncio.to_thread(_read_bounded_regular_file, path, max_bytes)
    header_offset = data[:1024].find(b"%PDF-")
    if header_offset < 0:
        raise MainBookFileError("The local file is not a PDF.")
    return await _validated_pdf_source(
        filename=path.name,
        data=data,
        max_pages=max_pages,
        header_offset=header_offset,
        local_path=path,
    )


def _is_within_allowed_root(path: Path, roots: Sequence[Path]) -> bool:
    """Compare ancestor directory identities without assuming case semantics."""
    root_stats = []
    for root in roots:
        try:
            root_stats.append(os.stat(root))
        except OSError:
            continue

    for parent in path.parents:
        try:
            parent_stat = os.stat(parent)
        except OSError:
            continue
        if any(os.path.samestat(parent_stat, root_stat) for root_stat in root_stats):
            return True
    return False


def expand_client_tokens(value: str) -> str:
    """Expand the bundle placeholders a host may hand us verbatim.

    Claude Desktop 1.26832.0 passes user_config defaults through unsubstituted: an installed
    bundle is launched with the literal argument "${HOME}/Downloads". Expanding here keeps the
    manifest in the documented form while surviving hosts that do not substitute. Unknown
    placeholders are left alone, so they simply fail the directory check.
    """
    home = Path.home()
    replacements = {
        "${HOME}": str(home),
        "${DESKTOP}": str(home / "Desktop"),
        "${DOCUMENTS}": str(home / "Documents"),
        "${DOWNLOADS}": str(home / "Downloads"),
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return value


def normalize_allowed_roots(roots: Sequence[str | Path]) -> tuple[Path, ...]:
    """Expand, resolve, de-duplicate, and discard roots that are not directories."""
    resolved_roots: list[Path] = []
    for candidate in roots:
        if not str(candidate).strip():
            continue
        try:
            root = Path(expand_client_tokens(str(candidate))).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if os.path.isdir(root) and root not in resolved_roots:
            resolved_roots.append(root)
    return tuple(resolved_roots)


def _read_bounded_regular_file(path: Path, max_bytes: int) -> bytes:
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        descriptor_stat = os.fstat(fd)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise MainBookFileError("The local PDF path must point to a regular file.")
        if descriptor_stat.st_size > max_bytes:
            raise MainBookFileError(f"The PDF is larger than the {max_bytes}-byte upload limit.")

        handle = os.fdopen(fd, "rb")
        fd = None
        with handle:
            data = handle.read(max_bytes + 1)
    except MainBookFileError:
        raise
    except OSError as exc:
        raise MainBookFileError("The local PDF was not found or cannot be read.") from exc
    finally:
        if fd is not None:
            os.close(fd)

    if len(data) > max_bytes:
        raise MainBookFileError(f"The PDF is larger than the {max_bytes}-byte upload limit.")
    return data


async def download_pdf_url(
    file_url: str,
    *,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
    timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
) -> PDFSource:
    """Download one public HTTPS URL without redirects and with two size gates."""
    try:
        parsed = urlsplit(file_url)
        port = parsed.port or 443
    except ValueError as exc:
        raise MainBookFileError("file_url is not a valid HTTPS URL.") from exc
    if parsed.scheme.lower() != "https":
        raise MainBookFileError("file_url must use HTTPS.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise MainBookFileError("file_url must be a public HTTPS URL without embedded credentials.")

    resolve = resolver or resolve_host_addresses
    try:
        addresses = await resolve(parsed.hostname, port)
    except (OSError, socket.gaierror) as exc:
        raise MainBookFileError("The file_url hostname could not be resolved.") from exc
    _require_public_addresses(addresses)
    original_url = httpx.URL(file_url)
    pinned_url = original_url.copy_with(host=addresses[0])

    timeout = httpx.Timeout(timeout_seconds)
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream(
                "GET",
                pinned_url,
                # The socket target stays numeric; the original name is used only for HTTP/TLS.
                headers={"Host": original_url.netloc.decode("ascii")},
                extensions={"sni_hostname": original_url.host},
            ) as response,
        ):
            if 300 <= response.status_code < 400:
                raise MainBookFileError(
                    "file_url redirects are not allowed; provide the final HTTPS URL."
                )
            if response.status_code != 200:
                raise MainBookFileError("The remote PDF link is unavailable.")

            declared_length = _content_length(response.headers.get("Content-Length"))
            if declared_length is not None and declared_length > max_bytes:
                raise MainBookFileError(
                    f"The remote PDF Content-Length is larger than the {max_bytes}-byte limit."
                )

            chunks: list[bytes] = []
            downloaded = 0
            async for chunk in response.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise MainBookFileError(
                        f"The remote PDF download exceeded the {max_bytes}-byte limit."
                    )
                chunks.append(chunk)
    except MainBookFileError:
        raise
    except httpx.TimeoutException as exc:
        raise MainBookFileError(
            "The remote PDF download timed out. Check the URL and retry."
        ) from exc
    except httpx.HTTPError as exc:
        raise MainBookFileError("The remote PDF could not be reached over HTTPS.") from exc

    data = b"".join(chunks)
    filename = _url_filename(parsed.path)
    return await _validated_pdf_source(filename=filename, data=data, max_pages=max_pages)


async def resolve_host_addresses(host: str, port: int) -> Sequence[str]:
    """Resolve every TCP address so the caller can fail closed on mixed answers."""
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise
    return tuple(record[4][0] for record in records)


def _require_public_addresses(addresses: Sequence[str]) -> None:
    if not addresses:
        raise MainBookFileError("file_url must resolve to a public Internet address.")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise MainBookFileError("file_url must resolve to a public Internet address.") from exc
        if not address.is_global:
            raise MainBookFileError("file_url must resolve to a public Internet address.")


async def _validated_pdf_source(
    *,
    filename: str,
    data: bytes,
    max_pages: int,
    header_offset: int = 0,
    local_path: Path | None = None,
) -> PDFSource:
    def inspect() -> int:
        try:
            # Strict pypdf requires the header at byte zero. Its recovery mode is needed for
            # legal pre-header bytes because xref offsets still count from the start of the file.
            reader = PdfReader(io.BytesIO(data), strict=header_offset == 0)
            return len(reader.pages)
        except (PdfReadError, ValueError, OSError, EOFError) as exc:
            raise MainBookFileError("The source is not a valid readable PDF.") from exc

    page_count = await asyncio.to_thread(inspect)
    if page_count < 1:
        raise MainBookFileError("The PDF has no pages.")
    if page_count > max_pages:
        raise MainBookFileError(f"The PDF has more than {max_pages} pages.")
    return PDFSource(
        filename=_safe_filename(filename),
        data=data,
        size_bytes=len(data),
        page_count=page_count,
        local_path=local_path,
    )


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    return length if length >= 0 else None


def _url_filename(path: str) -> str:
    name = unquote(path.rsplit("/", 1)[-1])
    return name or "statement.pdf"


def _safe_filename(filename: str) -> str:
    cleaned = "".join(char for char in filename if char >= " " and char not in {"/", "\\"}).strip()
    if not cleaned:
        cleaned = "statement.pdf"
    return cleaned[:255]
