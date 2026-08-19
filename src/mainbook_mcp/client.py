"""Thin asynchronous client for the public MainBook Developer REST API."""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx2 as httpx
import jwt

from . import __version__
from .errors import MainBookAPIError, MainBookNetworkError, api_error_from_response

#: How this server announces itself to the MainBook API. The backend normalises
#: the leading token to `Document.client_kind = "mcp"` so the operator alert can
#: say which channel a conversion came through — see
#: `apps/developer/client_kind.py`. Attribution only: nothing is granted on it.
USER_AGENT = f"mainbook-mcp/{__version__}"

TERMINAL_OR_ACTIONABLE_STATES = {
    "succeeded",
    "succeeded_with_warnings",
    "failed",
    "expired",
    "insufficient_credits",
}


@dataclass(frozen=True)
class PollOutcome:
    """Latest public job snapshot plus whether the caller's time budget ended."""

    job: dict[str, Any] | None
    timed_out: bool


@dataclass(frozen=True)
class ServiceCredentialIssuer:
    """Issue a fresh one-request credential for each call through Django's service door."""

    subject: uuid.UUID
    client_id: str
    signing_secret: str
    wall_clock: Callable[[], float] = time.time
    jwt_id: Callable[[], uuid.UUID] = uuid.uuid4
    auth_failure: Callable[[], None] | None = None

    def header_value(self) -> str:
        issued_at = int(self.wall_clock())
        return jwt.encode(
            {
                "iss": "mcp",
                "aud": "api.mainbook.ai",
                "sub": str(self.subject),
                "cid": self.client_id,
                "jti": str(self.jwt_id()),
                "iat": issued_at,
                "exp": issued_at + 60,
            },
            self.signing_secret,
            algorithm="HS256",
        )

    def mark_auth_failure(self) -> None:
        if self.auth_failure is not None:
            self.auth_failure()


DeveloperCredential = str | ServiceCredentialIssuer


class MainBookClient:
    """HTTP client that never shares credentials outside its own tool call."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        service_credential: ServiceCredentialIssuer | None = None,
        base_url: str = "https://api.mainbook.ai",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        clean_key = api_key.strip() if isinstance(api_key, str) else ""
        if not clean_key and service_credential is None:
            raise ValueError("MainBook API key is required")
        if clean_key and service_credential is not None:
            raise ValueError("Exactly one MainBook API credential is required")
        self._api_key = clean_key or None
        self._service_credential = service_credential
        self._developer_url = _developer_base_url(base_url)
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._jitter = jitter
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )

    async def __aenter__(self) -> MainBookClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_job(
        self,
        *,
        filename: str,
        size_bytes: int,
        page_count: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = await self._request_api(
            "POST",
            "/jobs",
            json_body={
                "filename": filename,
                "file_format": "pdf",
                "size_bytes": size_bytes,
                "page_count": page_count,
            },
            headers=headers,
        )
        return self._json_object(response)

    async def upload_pdf(self, upload: Mapping[str, Any], data: bytes) -> None:
        method = upload.get("method")
        url = upload.get("url")
        headers = upload.get("headers")
        if method != "PUT" or not isinstance(url, str) or not isinstance(headers, dict):
            raise MainBookNetworkError(
                "MainBook returned invalid upload instructions. Retry the conversion."
            )
        try:
            response = await self._http.request("PUT", url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise MainBookNetworkError(
                "Could not reach the upload storage. The job remains resumable until its upload expires."
            ) from exc
        if not 200 <= response.status_code < 300:
            raise MainBookNetworkError(
                "The upload storage rejected the PDF. Create a new conversion if the upload window expired."
            )

    async def start_job(self, job_id: str) -> dict[str, Any]:
        response = await self._request_api("POST", f"/jobs/{job_id}/start")
        return self._json_object(response)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        response = await self._request_api("GET", f"/jobs/{job_id}")
        return self._json_object(response)

    async def list_jobs(self, *, limit: int, cursor: str | None = None) -> dict[str, Any]:
        params = {"page_size": str(limit)}
        if cursor is not None:
            params["cursor"] = cursor
        response = await self._request_api("GET", "/jobs", params=params)
        return self._json_object(response)

    async def get_result(
        self,
        job_id: str,
        result_type: Literal["json", "xlsx", "csv"],
    ) -> dict[str, Any] | bytes:
        response = await self._request_api(
            "GET",
            f"/jobs/{job_id}/result",
            params={"type": result_type},
        )
        if result_type == "json":
            return self._json_object(response)
        return response.content

    async def get_balance(self) -> dict[str, Any]:
        response = await self._request_api("GET", "/balance")
        return self._json_object(response)

    async def poll_job(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        on_progress: Callable[[float, str], Awaitable[None]] | None = None,
    ) -> PollOutcome:
        deadline = self._monotonic() + timeout_seconds
        # Derived, not sampled again: the injected clock in tests yields a fixed sequence, and an
        # extra tick here would silently shift every deadline assertion in the poller tests.
        started = deadline - timeout_seconds
        latest: dict[str, Any] | None = None
        while True:
            if self._monotonic() >= deadline:
                return PollOutcome(job=latest, timed_out=True)
            try:
                latest = await self.get_job(job_id)
                if on_progress is not None:
                    state = latest.get("state")
                    await on_progress(
                        self._monotonic() - started,
                        state if isinstance(state, str) else "processing",
                    )
            except MainBookAPIError as exc:
                if exc.reason != "rate_limited":
                    raise
                delay = exc.retry_after if exc.retry_after is not None else 3.0
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return PollOutcome(job=latest, timed_out=True)
                await self._sleep(min(delay, remaining))
                continue

            if latest.get("state") in TERMINAL_OR_ACTIONABLE_STATES:
                return PollOutcome(job=latest, timed_out=False)

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return PollOutcome(job=latest, timed_out=True)
            await self._sleep(min(self._jitter(3.0, 5.0), remaining))

    async def _request_api(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: object | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        # Only on our own API calls: the presigned storage PUT in `upload_pdf`
        # is signed for a bare request and must not gain headers here.
        request_headers = {"User-Agent": USER_AGENT}
        if headers:
            request_headers.update(headers)
        # Authentication is selected last so a per-call header can never replace,
        # duplicate, or smuggle the client's credential into the internal request.
        request_headers.pop("Authorization", None)
        request_headers.pop("authorization", None)
        request_headers.pop("X-MainBook-Service", None)
        request_headers.pop("x-mainbook-service", None)
        if self._service_credential is not None:
            request_headers["X-MainBook-Service"] = self._service_credential.header_value()
        else:
            request_headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._http.request(
                method,
                f"{self._developer_url}{path}",
                params=params,
                json=json_body,
                headers=request_headers,
            )
        except httpx.HTTPError as exc:
            raise MainBookNetworkError(
                "The MCP server could not reach the MainBook API. Check connectivity and retry."
            ) from exc
        if 200 <= response.status_code < 300:
            return response
        if response.status_code == 401 and self._service_credential is not None:
            self._service_credential.mark_auth_failure()
        payload: object
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        raise api_error_from_response(
            status_code=response.status_code,
            payload=payload,
            retry_after=_retry_after_seconds(
                response.headers.get("Retry-After"), now=self._wall_clock()
            ),
        )

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MainBookNetworkError(
                "MainBook returned an unreadable response. Retry later."
            ) from exc
        if not isinstance(payload, dict):
            raise MainBookNetworkError("MainBook returned an unexpected response. Retry later.")
        return payload


def _developer_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/api/v1/developer"):
        return normalized
    if normalized.endswith("/api/v1"):
        return f"{normalized}/developer"
    return f"{normalized}/api/v1/developer"


def _retry_after_seconds(value: str | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - now).total_seconds())
