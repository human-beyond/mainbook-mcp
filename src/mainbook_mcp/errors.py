"""Sanitized, agent-actionable errors for MainBook MCP."""

from __future__ import annotations

from typing import Any


class MainBookError(RuntimeError):
    """Base class for errors safe to return through MCP."""


class MainBookNetworkError(MainBookError):
    """The REST service could not be reached or its response could not be read."""


class MainBookFileError(MainBookError):
    """A local or remote PDF source was unsafe or invalid."""


class MainBookAPIError(MainBookError):
    """A named refusal from the public MainBook Developer API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        reason: str,
        retry_after: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.retry_after = retry_after
        self.context = context or {}


def api_error_from_response(
    *,
    status_code: int,
    payload: object,
    retry_after: float | None,
) -> MainBookAPIError:
    """Translate the stable REST reason code without exposing raw server details."""
    body = payload if isinstance(payload, dict) else {}
    reason_value = body.get("reason")
    if status_code == 401 and body.get("detail") == "invalid_key":
        reason_value = "invalid_key"
    reason = reason_value if isinstance(reason_value, str) else "service_error"
    wait = retry_after if retry_after is not None else 0.0

    if reason == "invalid_key":
        message = (
            "The MainBook API key is invalid or revoked. Run mainbook-mcp auth login again or "
            "check MAINBOOK_API_KEY for stdio; check the Authorization header for HTTP mode."
        )
    elif reason == "insufficient_credits":
        available = _safe_int(body.get("available"))
        requested = _safe_int(body.get("requested"))
        message = (
            f"MainBook has {available} pages available but needs {requested}. "
            "Buy additional page credits at mainbook.ai, then retry."
        )
    elif reason == "api_terms_not_accepted":
        message = (
            "The account has not accepted the current API Terms. Accept them at "
            "mainbook.ai/developer, then retry."
        )
    elif reason == "engine_paused":
        message = "MainBook conversion is temporarily paused. Retry later."
    elif reason == "concurrency_limit":
        message = (
            f"The account already has six conversions in flight; retry after {wait:g} seconds."
        )
    elif reason == "rate_limited":
        message = f"The MainBook API rate limit was reached; retry after {wait:g} seconds."
    elif reason == "job_expired":
        message = "The upload reservation expired. Create a new conversion job and upload again."
    elif reason == "job_not_terminal":
        message = (
            "The conversion is not finished yet. Poll get_conversion after the suggested delay."
        )
    elif reason == "account_suspended":
        message = "The MainBook account is suspended. Resolve the account status at mainbook.ai."
    elif reason == "idempotency_conflict":
        message = (
            "This idempotency key was already used for a different request. Reuse the original "
            "request body or choose a new idempotency key."
        )
    elif reason == "not_found":
        message = "The conversion job was not found for this MainBook account. Check the job_id."
    elif reason == "invalid_request":
        message = "MainBook rejected the request as invalid. Check the PDF and input limits."
    else:
        message = f"MainBook could not complete the request (HTTP {status_code}). Retry later."

    return MainBookAPIError(
        message,
        status_code=status_code,
        reason=reason,
        retry_after=retry_after,
        context={
            key: body[key]
            for key in ("available", "requested", "state", "limit", "size_bytes")
            if key in body
        },
    )


def _safe_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
