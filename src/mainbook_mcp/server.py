"""Official-SDK MCP server exposing MainBook conversion tools."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlsplit

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .client import DeveloperCredential, MainBookClient, ServiceCredentialIssuer
from .credentials import CredentialError, load_credential, resolve_api_base
from .errors import MainBookError, MainBookFileError, MainBookNetworkError
from .files import PDFSource, download_pdf_url, load_local_pdf, normalize_allowed_roots
from .models import (
    DEFAULT_POLL_SECONDS,
    BalanceOutput,
    ConversionData,
    ConversionOutput,
    ConvertBankStatementInput,
    DownloadInstruction,
    GetConversionInput,
    Job,
    ListConversionsInput,
    ListConversionsOutput,
    OutputFolderOutput,
    ResultType,
    SavedFile,
)
from .oauth_http import (
    OAuthToolAuthMiddleware,
    current_oauth_request_state,
    decode_internal_identity,
)
from .oauth_verifier import SUPPORTED_SCOPES, OAuthSettings, OAuthTokenVerifier
from .output import (
    NEXT_TO_SOURCE,
    OutputDestination,
    prepare_output_path,
    read_output_preference,
    serialized_json_bytes,
    validate_preference_folder,
    write_output_preference,
    write_result_bytes,
)

ClientFactory = Callable[[DeveloperCredential, str], AbstractAsyncContextManager[Any]]
SourceLoader = Callable[[ConvertBankStatementInput], Awaitable[PDFSource]]

CONVERT_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
READ_ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=True)
GET_CONVERSION_LOCAL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
GET_CONVERSION_HOSTED_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
OUTPUT_FOLDER_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)


class OAuthAwareMCPServer(MCPServer):
    """Wrap only Streamable HTTP with lazy per-tool OAuth authentication."""

    def __init__(self, *args: Any, oauth_verifier: OAuthTokenVerifier, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mainbook_oauth_verifier = oauth_verifier

    def streamable_http_app(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        app = super().streamable_http_app(**kwargs)
        return OAuthToolAuthMiddleware(app, self._mainbook_oauth_verifier)


def create_server(
    *,
    transport: Literal["stdio", "http"],
    allowed_roots: Sequence[str | Path] = (),
    client_factory: ClientFactory | None = None,
    source_loader: SourceLoader | None = None,
    api_base: str | None = None,
    oauth_settings: OAuthSettings | None = None,
    oauth_verifier: OAuthTokenVerifier | None = None,
) -> MCPServer:
    """Build an isolated server; injected dependencies keep contract tests deterministic."""
    make_client = client_factory or _default_client_factory
    active_roots = normalize_allowed_roots(allowed_roots)
    resolved_api_base = resolve_api_base(api_base)
    active_oauth = (
        oauth_settings
        if oauth_settings is not None
        else (OAuthSettings.from_env() if transport == "http" else OAuthSettings())
    )
    active_oauth.validate()
    verifier = oauth_verifier
    if transport == "http" and active_oauth.enabled and verifier is None:
        verifier = OAuthTokenVerifier(active_oauth)

    async def load_default_source(request: ConvertBankStatementInput) -> PDFSource:
        if request.file_path is not None:
            return await load_local_pdf(request.file_path, allowed_roots=active_roots)
        return await _default_source_loader(request)

    load_source = source_loader or load_default_source

    def request_credential(ctx: Context) -> DeveloperCredential:
        if active_oauth.enabled:
            return _api_key_for_request(
                ctx,
                transport=transport,
                api_base=resolved_api_base,
                oauth_settings=active_oauth,
            )
        return _api_key_for_request(ctx, transport=transport, api_base=resolved_api_base)

    server_type = (
        OAuthAwareMCPServer if verifier is not None and active_oauth.enabled else MCPServer
    )
    server_kwargs: dict[str, Any] = {}
    if server_type is OAuthAwareMCPServer:
        server_kwargs["oauth_verifier"] = verifier
    server = server_type(
        name="mainbook",
        title="MainBook Bank Statement Converter",
        description=(
            "Convert PDF bank statements to checked Excel, CSV or JSON with balance validation."
        ),
        instructions=(
            "Use convert_bank_statement for a new PDF. It creates a paid page-credit job, so do "
            "not call it speculatively. Use get_conversion after a timeout."
        ),
        version=__version__,
        **server_kwargs,
    )

    if transport == "http" and active_oauth.enabled:

        @server.custom_route(
            "/.well-known/oauth-protected-resource/mcp",
            methods=["GET"],
        )
        async def protected_resource_metadata(request: Request) -> JSONResponse:
            del request
            return JSONResponse(
                {
                    "resource": active_oauth.resource,
                    "authorization_servers": [active_oauth.issuer],
                    "scopes_supported": list(SUPPORTED_SCOPES),
                    "bearer_methods_supported": ["header"],
                },
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "public, max-age=3600",
                },
            )

    @server.tool(
        title="Convert bank statement",
        annotations=CONVERT_ANNOTATIONS,
        description=(
            "Convert one PDF bank statement through the complete MainBook workflow: create a job, "
            "upload, start, poll, and return structured data. This creates a job and spends page "
            "credits; it is not read-only."
        ),
    )
    async def convert_bank_statement(
        ctx: Context,
        file_path: Annotated[
            str | None,
            Field(
                min_length=1,
                description=(
                    "Path to a PDF on the MCP server machine. This field is only available over "
                    "stdio and is rejected in HTTP mode; remote clients must use file_url. "
                    "The path must be inside the allowed folders, which default to Downloads, "
                    "Desktop, and Documents. Exactly one of file_path and file_url is required."
                ),
            ),
        ] = None,
        file_url: Annotated[
            str | None,
            Field(
                min_length=1,
                description=(
                    "Public HTTPS URL of a PDF for remote mode. Redirects and non-public network "
                    "addresses are rejected. Exactly one source is required."
                ),
            ),
        ] = None,
        result_type: Annotated[
            Literal["json", "xlsx", "csv"],
            Field(
                description=(
                    "JSON is returned inline. Over stdio, XLSX or CSV is written to an allowed "
                    "local folder and the full path is returned. HTTP mode returns safe download "
                    "instructions. Binary bytes never enter model context."
                )
            ),
        ] = "json",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=30,
                le=900,
                description=(
                    "Internal polling budget from 30 to 900 seconds. Timeout leaves the job running "
                    "and returns its job_id for get_conversion. The default stays under the "
                    "60-second request timeout most MCP clients enforce; a client that gives up "
                    "first discards the job_id and the conversion looks lost."
                ),
            ),
        ] = DEFAULT_POLL_SECONDS,
        idempotency_key: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=255,
                description="Optional value forwarded verbatim in the Idempotency-Key REST header.",
            ),
        ] = None,
        output_path: Annotated[
            str | None,
            Field(
                min_length=1,
                description=(
                    "Optional absolute result file or existing folder on the MCP server machine. "
                    "Only available over stdio and only inside the allowed folders. The file "
                    "extension is corrected to match result_type."
                ),
            ),
        ] = None,
    ) -> ConversionOutput:
        request = ConvertBankStatementInput(
            file_path=file_path,
            file_url=file_url,
            result_type=result_type,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            output_path=output_path,
        )
        if transport == "http" and request.file_path is not None:
            raise MainBookFileError(
                "Local paths are only available over stdio. For remote HTTP connections, "
                "provide file_url."
            )
        if transport == "http" and request.output_path is not None:
            raise MainBookFileError(
                "output_path is only available over stdio because an HTTP server disk does not "
                "belong to the remote client."
            )
        # Credentials are settled before any file is read or fetched. Over HTTP the
        # caller is untrusted, so an anonymous or unusable key must never cost us a
        # disk read or 50 MB of egress; holding *a* string is not authentication,
        # hence the one cheap balance call that makes the API itself judge the key.
        api_key = request_credential(ctx)
        base_url = resolved_api_base
        async with make_client(api_key, base_url) as api:
            if transport == "http":
                await api.get_balance()
            source = await load_source(request)
            destination = await _destination_for_conversion(
                request=request,
                source=source,
                allowed_roots=active_roots,
                transport=transport,
            )
            created = await api.create_job(
                filename=source.filename,
                size_bytes=source.size_bytes,
                page_count=source.page_count,
                idempotency_key=request.idempotency_key,
            )
            job_id = _required_string(created, "job_id")
            upload = created.get("upload")
            if isinstance(upload, Mapping):
                await api.upload_pdf(upload, source.data)
                await api.start_job(job_id)
            else:
                existing = await api.get_job(job_id)
                if existing.get("state") in {"awaiting_upload", "expired"}:
                    raise MainBookError(
                        "The idempotent job no longer has a usable upload URL. Retry with a new "
                        "idempotency key."
                    )
            outcome = await api.poll_job(
                job_id,
                timeout_seconds=request.timeout_seconds,
                on_progress=_progress_reporter(ctx, request.timeout_seconds),
            )
            if outcome.job is None:
                return ConversionOutput(
                    job_id=job_id,
                    state=None,
                    pages=None,
                    validation=None,
                    result_type=request.result_type,
                    timed_out=True,
                    message=(
                        "Polling ended before MainBook returned a job status. Use get_conversion "
                        f"with job_id {job_id} to retrieve it later."
                    ),
                )
            return await _output_for_job(
                api=api,
                raw_job=outcome.job,
                result_type=request.result_type,
                timed_out=outcome.timed_out,
                base_url=base_url,
                transport=transport,
                destination=destination,
                allowed_roots=active_roots,
            )

    @server.tool(
        title="Get page-credit balance",
        annotations=READ_ANNOTATIONS,
        description=(
            "Return total, reserved, and available MainBook credits. Every value is measured in "
            "PDF pages."
        ),
    )
    async def get_balance(ctx: Context) -> BalanceOutput:
        api_key = request_credential(ctx)
        async with make_client(api_key, resolved_api_base) as api:
            raw = await api.get_balance()
        try:
            return BalanceOutput(
                balance=raw["balance"],
                reserved=raw["reserved"],
                available=raw["available"],
                explanation="All values are page credits; one page credit converts one PDF page.",
            )
        except (KeyError, ValidationError) as exc:
            raise MainBookNetworkError(
                "MainBook returned an unexpected balance response. Retry later."
            ) from exc

    @server.tool(
        title="List conversions",
        annotations=READ_ANNOTATIONS,
        description=(
            "List one cursor page of conversion jobs visible to the MainBook account. Pass the "
            "returned next_cursor to continue."
        ),
    )
    async def list_conversions(
        ctx: Context,
        limit: Annotated[
            int,
            Field(ge=1, le=100, description="Jobs on this page, from 1 to 100."),
        ] = 25,
        cursor: Annotated[
            str | None,
            Field(min_length=1, description="Opaque next_cursor from the previous page."),
        ] = None,
    ) -> ListConversionsOutput:
        request = ListConversionsInput(limit=limit, cursor=cursor)
        api_key = request_credential(ctx)
        async with make_client(api_key, resolved_api_base) as api:
            raw = await api.list_jobs(limit=request.limit, cursor=request.cursor)
        results = raw.get("results")
        if not isinstance(results, list):
            raise MainBookNetworkError(
                "MainBook returned an unexpected jobs response. Retry later."
            )
        try:
            conversions = [Job.model_validate(item) for item in results]
        except ValidationError as exc:
            raise MainBookNetworkError(
                "MainBook returned an unexpected job record. Retry later."
            ) from exc
        return ListConversionsOutput(
            conversions=conversions,
            next_cursor=_cursor_from_next(raw.get("next")),
            count=len(conversions),
        )

    @server.tool(
        title="Get conversion",
        annotations=(
            GET_CONVERSION_HOSTED_ANNOTATIONS
            if transport == "http"
            else GET_CONVERSION_LOCAL_ANNOTATIONS
        ),
        description=(
            "Get the current state of one MainBook conversion. When successful, return JSON inline "
            "or save XLSX/CSV locally over stdio. HTTP mode returns safe download instructions. "
            "Use this after convert_bank_statement times out."
        ),
    )
    async def get_conversion(
        ctx: Context,
        job_id: Annotated[
            str,
            Field(min_length=1, description="Conversion job UUID returned by MainBook."),
        ],
        result_type: Annotated[
            Literal["json", "xlsx", "csv"],
            Field(description="Result representation to retrieve after the job succeeds."),
        ] = "json",
        output_path: Annotated[
            str | None,
            Field(
                min_length=1,
                description=(
                    "Optional absolute result file or existing folder on the MCP server machine. "
                    "Only available over stdio and only inside the allowed folders."
                ),
            ),
        ] = None,
    ) -> ConversionOutput:
        request = GetConversionInput(
            job_id=job_id,
            result_type=result_type,
            output_path=output_path,
        )
        if transport == "http" and request.output_path is not None:
            raise MainBookFileError(
                "output_path is only available over stdio because an HTTP server disk does not "
                "belong to the remote client."
            )
        destination_request = await _destination_request_for_get(
            request=request,
            allowed_roots=active_roots,
            transport=transport,
        )
        api_key = request_credential(ctx)
        base_url = resolved_api_base
        async with make_client(api_key, base_url) as api:
            raw_job = await api.get_job(request.job_id)
            current = _validated_job(raw_job)
            destination = _materialize_destination(
                destination_request,
                result_type=request.result_type,
                filename=current.filename,
                allowed_roots=active_roots,
            )
            return await _output_for_job(
                api=api,
                raw_job=current,
                result_type=request.result_type,
                timed_out=False,
                base_url=base_url,
                transport=transport,
                destination=destination,
                allowed_roots=active_roots,
            )

    async def output_folder(
        path: Annotated[
            str | None,
            Field(
                min_length=1,
                description=(
                    "Allowed absolute folder to remember, or 'next_to_source' to reset. Omit to "
                    "read without changing anything."
                ),
            ),
        ] = None,
    ) -> OutputFolderOutput:
        if transport == "http":
            raise MainBookFileError(
                "output_folder is a local stdio setting and is unavailable in HTTP mode."
            )
        if path is not None:
            if path == NEXT_TO_SOURCE:
                stored = NEXT_TO_SOURCE
            else:
                folder = await asyncio.to_thread(validate_preference_folder, path, active_roots)
                stored = str(folder)
            await asyncio.to_thread(write_output_preference, stored)

        preference = await asyncio.to_thread(read_output_preference, active_roots)
        current = str(preference.folder) if preference.folder is not None else NEXT_TO_SOURCE
        allowed = [str(root) for root in active_roots]
        if preference.ignored:
            message = (
                "The saved folder is missing or no longer allowed, so results default next to "
                "the source PDF. Allowed folders: " + (_displayed_roots(active_roots))
            )
        elif preference.folder is not None:
            message = (
                f"The default output folder is {preference.folder}. Allowed folders: "
                + _displayed_roots(active_roots)
            )
        else:
            message = (
                "Results default next to the source PDF. Allowed folders: "
                + _displayed_roots(active_roots)
            )
        return OutputFolderOutput(
            output_folder=current,
            allowed_folders=allowed,
            message=message,
        )

    if transport == "stdio":
        server.tool(
            title="Manage output folder",
            annotations=OUTPUT_FOLDER_ANNOTATIONS,
            description=(
                "Read or change the default local result folder. Call with no path to inspect the "
                "current setting and allowed folders. Pass an allowed absolute folder, or "
                "'next_to_source' to restore the default behavior."
            ),
        )(output_folder)

    return server


def _progress_reporter(
    ctx: Context,
    total_seconds: int,
) -> Callable[[float, str], Awaitable[None]]:
    """Stream poll progress so clients that extend their timeout on it keep waiting.

    Progress is a courtesy, never a dependency: a client that ignores or rejects it must not turn
    a paid, already-running conversion into an error, so every failure here is swallowed.
    """

    async def report(elapsed: float, state: str) -> None:
        try:
            await ctx.report_progress(
                progress=min(elapsed, float(total_seconds)),
                total=float(total_seconds),
                message=f"MainBook conversion is {state}",
            )
        except Exception:  # see docstring: progress must never fail a running job
            return

    return report


async def _default_source_loader(request: ConvertBankStatementInput) -> PDFSource:
    if request.file_path is not None:
        return await load_local_pdf(request.file_path)
    if request.file_url is None:  # protected by the model validator
        raise MainBookError("Exactly one PDF source is required.")
    return await download_pdf_url(request.file_url)


def _default_client_factory(credential: DeveloperCredential, base_url: str) -> MainBookClient:
    if isinstance(credential, ServiceCredentialIssuer):
        return MainBookClient(service_credential=credential, base_url=base_url)
    return MainBookClient(api_key=credential, base_url=base_url)


def _api_key_for_request(
    ctx: Context,
    *,
    transport: Literal["stdio", "http"],
    api_base: str,
    oauth_settings: OAuthSettings | None = None,
) -> DeveloperCredential:
    if transport == "http":
        internal_identity = _header(ctx.headers, "x-mainbook-internal-oauth")
        if internal_identity is not None and oauth_settings is not None and oauth_settings.enabled:
            identity = decode_internal_identity(internal_identity)
            if identity is None:
                raise MainBookError("HTTP tool authentication failed.")
            raw_subject, client_id, consent_id = identity
            try:
                subject = uuid.UUID(raw_subject)
            except ValueError:
                raise MainBookError("HTTP tool authentication failed.") from None
            return ServiceCredentialIssuer(
                subject=subject,
                client_id=client_id,
                consent_id=consent_id,
                signing_secret=oauth_settings.service_signing_secret,
                auth_failure=(
                    state.mark_downstream_auth_failed
                    if (state := current_oauth_request_state.get()) is not None
                    else None
                ),
            )
        authorization = _header(ctx.headers, "authorization")
        if authorization is None:
            raise MainBookError("HTTP tool calls require an Authorization: Bearer <key> header.")
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            raise MainBookError("Authorization must use a non-empty Bearer API key.")
        credential = token.strip()
        if (
            oauth_settings is not None
            and oauth_settings.enabled
            and not credential.startswith("mb_live_")
        ):
            raise MainBookError("HTTP tool authentication failed.")
        return credential
    fallback = os.getenv("MAINBOOK_API_KEY", "").strip()
    if fallback:
        return fallback
    try:
        stored = load_credential(api_base)
    except CredentialError as exc:
        raise MainBookError(
            "The stored MainBook credential could not be read. Run 'mainbook-mcp auth login' again."
        ) from exc
    if stored is not None:
        return stored.api_key
    raise MainBookError(
        "No MainBook credential is configured. Run 'mainbook-mcp auth login', or set "
        "MAINBOOK_API_KEY for scripts and CI."
    )


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    for key, value in headers.items():
        if key.casefold() == name.casefold():
            return value
    return None


DestinationRequest = tuple[str | Path, str, str | None]


async def _destination_for_conversion(
    *,
    request: ConvertBankStatementInput,
    source: PDFSource,
    allowed_roots: tuple[Path, ...],
    transport: Literal["stdio", "http"],
) -> OutputDestination | None:
    if transport == "http" or (request.result_type == "json" and request.output_path is None):
        return None

    warning: str | None = None
    if request.output_path is not None:
        requested: str | Path = request.output_path
        reason = "placed at the output_path requested for this call"
    else:
        preference = await asyncio.to_thread(read_output_preference, allowed_roots)
        if preference.folder is not None:
            requested = preference.folder
            reason = "placed in the configured default output folder"
        else:
            if source.local_path is None:
                raise MainBookFileError(
                    "I do not know where to place this result. Pass output_path or set a default "
                    "output folder with output_folder."
                )
            requested = source.local_path.parent
            reason = "placed next to the source PDF"
            if preference.ignored:
                warning = "The saved output folder was ignored because it is missing or no longer allowed."

    path = await asyncio.to_thread(
        prepare_output_path,
        requested,
        result_type=request.result_type,
        default_filename=source.filename,
        allowed_roots=allowed_roots,
    )
    return OutputDestination(path=path, reason=reason, warning=warning)


async def _destination_request_for_get(
    *,
    request: GetConversionInput,
    allowed_roots: tuple[Path, ...],
    transport: Literal["stdio", "http"],
) -> DestinationRequest | None:
    if transport == "http" or (request.result_type == "json" and request.output_path is None):
        return None
    if not allowed_roots:
        raise MainBookFileError(
            "Results may only be written inside the allowed folders: (none active)"
        )

    if request.output_path is not None:
        # Validate the real parent before making even the status request. The final name is
        # prepared again after the job supplies its original PDF filename.
        await asyncio.to_thread(
            prepare_output_path,
            request.output_path,
            result_type=request.result_type,
            default_filename="result.pdf",
            allowed_roots=allowed_roots,
        )
        return (
            request.output_path,
            "placed at the output_path requested for this call",
            None,
        )

    preference = await asyncio.to_thread(read_output_preference, allowed_roots)
    if preference.folder is not None:
        return (
            preference.folder,
            "placed in the configured default output folder",
            None,
        )
    ignored = (
        " The saved output folder was ignored because it is missing or no longer allowed."
        if preference.ignored
        else ""
    )
    raise MainBookFileError(
        "I do not know where to place this result. Pass output_path or set a default output "
        f"folder with output_folder.{ignored}"
    )


def _materialize_destination(
    request: DestinationRequest | None,
    *,
    result_type: ResultType,
    filename: str,
    allowed_roots: tuple[Path, ...],
) -> OutputDestination | None:
    if request is None:
        return None
    requested, reason, warning = request
    return OutputDestination(
        path=prepare_output_path(
            requested,
            result_type=result_type,
            default_filename=filename,
            allowed_roots=allowed_roots,
        ),
        reason=reason,
        warning=warning,
    )


def _validated_job(raw_job: Mapping[str, Any] | Job) -> Job:
    if isinstance(raw_job, Job):
        return raw_job
    try:
        return Job.model_validate(raw_job)
    except ValidationError as exc:
        raise MainBookNetworkError(
            "MainBook returned an unexpected job response. Retry later."
        ) from exc


async def _output_for_job(
    *,
    api: Any,
    raw_job: Mapping[str, Any] | Job,
    result_type: ResultType,
    timed_out: bool,
    base_url: str,
    transport: Literal["stdio", "http"],
    destination: OutputDestination | None,
    allowed_roots: tuple[Path, ...],
) -> ConversionOutput:
    current = _validated_job(raw_job)

    if timed_out:
        return ConversionOutput(
            job_id=current.job_id,
            state=current.state,
            pages=current.pages,
            validation=current.validation,
            result_type=result_type,
            timed_out=True,
            message=(
                f"Conversion is still processing. Use get_conversion with job_id {current.job_id} "
                "to retrieve it later."
            ),
        )
    if current.state == "failed":
        raise MainBookError(
            f"MainBook conversion {current.job_id} failed. Retry with a new job; contact support "
            "if the same PDF fails again."
        )
    if current.state == "insufficient_credits":
        raise MainBookError(
            f"MainBook conversion {current.job_id} needs more page credits. Buy pages at "
            "mainbook.ai, then retry the job."
        )
    if current.state == "expired":
        raise MainBookError(
            f"MainBook conversion {current.job_id} expired before upload. Create a new conversion job."
        )
    if current.state not in {"succeeded", "succeeded_with_warnings"}:
        return ConversionOutput(
            job_id=current.job_id,
            state=current.state,
            pages=current.pages,
            validation=current.validation,
            result_type=result_type,
            message=(
                f"Conversion is {current.state}. Call get_conversion again with job_id "
                f"{current.job_id}."
            ),
        )

    if result_type == "json":
        raw_result = await api.get_result(current.job_id, "json")
        try:
            data = ConversionData.model_validate(raw_result)
        except ValidationError as exc:
            raise MainBookNetworkError(
                "MainBook returned an unexpected JSON export. Retry later."
            ) from exc
        saved_file: SavedFile | None = None
        message = "Conversion completed; structured JSON is included in data."
        if destination is not None:
            written = await asyncio.to_thread(
                write_result_bytes,
                destination.path,
                serialized_json_bytes(data.model_dump(mode="json")),
                allowed_roots,
            )
            saved_file = SavedFile(path=str(written), reason=destination.reason)
            message = _saved_message(
                destination,
                written,
                prefix="Conversion completed; structured JSON is included in data.",
            )
        return ConversionOutput(
            job_id=current.job_id,
            state=current.state,
            pages=current.pages,
            validation=current.validation,
            result_type=result_type,
            data=data,
            saved_file=saved_file,
            message=message,
        )

    if transport == "stdio":
        if destination is None:  # defensive: destination selection fails earlier
            raise MainBookFileError(
                "I do not know where to place this result. Pass output_path or set a default "
                "output folder with output_folder."
            )
        raw_result = await api.get_result(current.job_id, result_type)
        if not isinstance(raw_result, bytes):
            raise MainBookNetworkError(
                f"MainBook returned an unexpected {result_type.upper()} export. Retry later."
            )
        written = await asyncio.to_thread(
            write_result_bytes, destination.path, raw_result, allowed_roots
        )
        return ConversionOutput(
            job_id=current.job_id,
            state=current.state,
            pages=current.pages,
            validation=current.validation,
            result_type=result_type,
            saved_file=SavedFile(path=str(written), reason=destination.reason),
            message=_saved_message(destination, written, prefix="Conversion completed."),
        )

    return ConversionOutput(
        job_id=current.job_id,
        state=current.state,
        pages=current.pages,
        validation=current.validation,
        result_type=result_type,
        download=DownloadInstruction(
            job_id=current.job_id,
            result_type=result_type,
            rest_endpoint=_result_endpoint(base_url, current.job_id, result_type),
            instruction=(
                f"Call get_conversion with this job_id and result_type='{result_type}' to confirm "
                "status, then download the binary from the REST endpoint using the same Bearer key."
            ),
        ),
        message=f"Conversion completed. Binary {result_type.upper()} was not embedded in MCP output.",
    )


def _saved_message(destination: OutputDestination, path: Path, *, prefix: str) -> str:
    warning = f"{destination.warning} " if destination.warning else ""
    return f"{warning}{prefix} Wrote the file to {path} because it was {destination.reason}."


def _displayed_roots(roots: tuple[Path, ...]) -> str:
    return ", ".join(str(root) for root in roots) or "(none active)"


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MainBookNetworkError(
            "MainBook returned an unexpected create-job response. Retry later."
        )
    return value


def _cursor_from_next(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    cursors = parse_qs(urlsplit(value).query).get("cursor")
    return cursors[0] if cursors else None


def _result_endpoint(base_url: str, job_id: str, result_type: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/api/v1/developer"):
        root = normalized
    elif normalized.endswith("/api/v1"):
        root = f"{normalized}/developer"
    else:
        root = f"{normalized}/api/v1/developer"
    return f"{root}/jobs/{job_id}/result?type={result_type}"
