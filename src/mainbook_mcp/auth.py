"""Browser-assisted PKCE device authorization for the command line."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import time
import unicodedata
import webbrowser
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx

from .client import USER_AGENT
from .credentials import normalize_api_base

PKCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class AuthFlowError(RuntimeError):
    """A sanitized, user-actionable terminal sign-in failure."""


@dataclass(frozen=True)
class DeviceToken:
    """One-time successful device exchange; callers must store it without displaying it."""

    api_key: str
    client_name: str


def create_pkce_pair() -> tuple[str, str]:
    """Generate the exact 32-byte, unpadded base64url S256 pair required by the backend."""
    verifier = _base64url(secrets.token_bytes(32))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def detect_client_name(
    *,
    environ: Mapping[str, str] | None = None,
    hostname: str | None = None,
) -> str:
    """Describe only signals the running process can reasonably observe."""
    current = os.environ if environ is None else environ
    if current.get("CLAUDECODE"):
        client = "Claude Code"
    elif current.get("CODEX_THREAD_ID") or current.get("CODEX_HOME"):
        client = "Codex"
    elif current.get("CURSOR_TRACE_ID"):
        client = "Cursor"
    elif current.get("VSCODE_PID"):
        client = "VS Code"
    else:
        term_program = current.get("TERM_PROGRAM", "").strip()
        known_terminals = {
            "Apple_Terminal": "Terminal",
            "iTerm.app": "iTerm",
            "WezTerm": "WezTerm",
        }
        client = known_terminals.get(term_program, "MainBook MCP")
    machine = hostname if hostname is not None else socket.gethostname()
    clean_machine = _clean_client_name(machine) or "this machine"
    return _clean_client_name(f"{client} on {clean_machine}")[:100]


class DeviceAuthFlow:
    """Execute the public start/open/poll protocol without ever rendering the credential."""

    def __init__(
        self,
        *,
        api_base: str,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        open_browser: Callable[[str], bool] = webbrowser.open,
        write: Callable[[str], None] = print,
    ) -> None:
        self._api_base = normalize_api_base(api_base)
        self._transport = transport
        self._sleep = sleep
        self._monotonic = monotonic
        self._open_browser = open_browser
        self._write = write

    async def run(self, *, client_name: str, no_browser: bool = False) -> DeviceToken:
        clean_name = _clean_client_name(client_name)[:100]
        if not clean_name:
            raise AuthFlowError("Could not determine a usable client name for this sign-in.")
        verifier, challenge = create_pkce_pair()
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            start = await self._post(
                client,
                "/api/v1/developer/device/start",
                {"code_challenge": challenge, "client_name": clean_name},
            )
            if start.status_code == 429:
                raise AuthFlowError(
                    "Too many sign-in attempts. Wait a moment and run the command again."
                )
            if start.status_code != 200:
                raise AuthFlowError(
                    "MainBook could not start terminal sign-in. Check the API base and retry."
                )
            start_payload = _json_object(start)
            device_code, verification_uri, expires_in, interval = _validated_start(start_payload)
            short_code = _short_code(verification_uri)

            self._write(f"Sign-in code: {short_code}")
            self._write("Confirm that this code matches the code shown by MainBook in the browser.")
            if no_browser:
                self._write(f"Open this URL: {verification_uri}")
            else:
                try:
                    opened = self._open_browser(verification_uri)
                except Exception:
                    opened = False
                if opened:
                    self._write("Opened MainBook in your browser. Waiting for approval…")
                else:
                    self._write(f"The browser did not open. Open this URL: {verification_uri}")

            return await self._poll(
                client,
                device_code=device_code,
                verifier=verifier,
                expires_in=expires_in,
                interval=interval,
            )

    async def _poll(
        self,
        client: httpx.AsyncClient,
        *,
        device_code: str,
        verifier: str,
        expires_in: int,
        interval: int,
    ) -> DeviceToken:
        deadline = self._monotonic() + expires_in
        poll_interval = float(interval)
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise _local_expiry()
            await self._sleep(min(poll_interval, remaining))
            if self._monotonic() >= deadline:
                raise _local_expiry()
            response = await self._post(
                client,
                "/api/v1/developer/device/token",
                {"device_code": device_code, "code_verifier": verifier},
            )
            payload = _json_object(response)
            if response.status_code == 200:
                api_key = payload.get("api_key")
                client_name = payload.get("client_name")
                if not isinstance(api_key, str) or not api_key.strip():
                    raise AuthFlowError(
                        "MainBook returned an incomplete sign-in response. Run login again."
                    )
                if not isinstance(client_name, str) or not client_name.strip():
                    raise AuthFlowError(
                        "MainBook returned an incomplete sign-in response. Run login again."
                    )
                return DeviceToken(api_key=api_key.strip(), client_name=client_name.strip())

            error = payload.get("error") if response.status_code == 400 else None
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                poll_interval += 5.0
                continue
            if error == "access_denied":
                raise AuthFlowError("Sign-in was denied in the browser. No credential was stored.")
            if error == "expired_token":
                raise _local_expiry()
            raise AuthFlowError(
                "MainBook could not complete terminal sign-in. Run the command again."
            )

    async def _post(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: dict[str, str],
    ) -> httpx.Response:
        try:
            return await client.post(f"{self._api_base}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise AuthFlowError(
                "Could not reach MainBook. Check connectivity and run login again."
            ) from exc


async def perform_login(
    *,
    api_base: str,
    client_name: str,
    no_browser: bool,
    write: Callable[[str], None] = print,
) -> DeviceToken:
    """CLI-facing wrapper kept injectable for deterministic command tests."""
    return await DeviceAuthFlow(api_base=api_base, write=write).run(
        client_name=client_name,
        no_browser=no_browser,
    )


async def check_credential(
    *,
    api_base: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Return whether the presented key is active without touching credits."""
    response = await _credential_lifecycle_request(
        "GET",
        api_base=api_base,
        api_key=api_key,
        transport=transport,
    )
    if response.status_code == 401:
        return False
    if response.status_code != 200:
        raise AuthFlowError("MainBook could not verify this credential. Try again later.")
    payload = _json_object(response)
    if payload.get("active") is not True:
        raise AuthFlowError("MainBook returned an unexpected credential status.")
    return True


async def revoke_credential(
    *,
    api_base: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Revoke only the key used for this request; false means it was already invalid."""
    response = await _credential_lifecycle_request(
        "DELETE",
        api_base=api_base,
        api_key=api_key,
        transport=transport,
    )
    if response.status_code == 401:
        return False
    if response.status_code != 204:
        raise AuthFlowError("MainBook could not revoke this credential. Try again later.")
    return True


async def _credential_lifecycle_request(
    method: str,
    *,
    api_base: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.Response:
    normalized_base = normalize_api_base(api_base)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "User-Agent": USER_AGENT,
            },
        ) as client:
            return await client.request(method, f"{normalized_base}/api/v1/developer/key")
    except httpx.HTTPError as exc:
        raise AuthFlowError("Could not reach MainBook to manage this credential.") from exc


def _validated_start(payload: dict[str, Any]) -> tuple[str, str, int, int]:
    device_code = payload.get("device_code")
    verification_uri = payload.get("verification_uri")
    expires_in = payload.get("expires_in")
    interval = payload.get("interval")
    if (
        not isinstance(device_code, str)
        or PKCE_PATTERN.fullmatch(device_code) is None
        or not isinstance(verification_uri, str)
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or expires_in <= 0
        or not isinstance(interval, int)
        or isinstance(interval, bool)
        or interval <= 0
    ):
        raise AuthFlowError("MainBook returned an unexpected sign-in response. Run login again.")
    _short_code(verification_uri)
    return device_code, verification_uri, expires_in, interval


def _short_code(verification_uri: str) -> str:
    try:
        parsed = urlsplit(verification_uri)
    except ValueError as exc:
        raise AuthFlowError(
            "MainBook returned an invalid verification URL. Run login again."
        ) from exc
    values = parse_qs(parsed.query).get("code")
    code = values[0].strip() if values else ""
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if (
        (parsed.scheme != "https" and not local_http)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not re.fullmatch(r"[A-Za-z0-9]{9}", code)
    ):
        raise AuthFlowError("MainBook returned an invalid verification URL. Run login again.")
    return code


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeError):
        raise AuthFlowError(
            "MainBook returned an unreadable sign-in response. Run login again."
        ) from None
    if not isinstance(payload, dict):
        raise AuthFlowError("MainBook returned an unexpected sign-in response. Run login again.")
    return payload


def _clean_client_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    without_controls = "".join(character for character in normalized if character.isprintable())
    return " ".join(without_controls.split())


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _local_expiry() -> AuthFlowError:
    return AuthFlowError("The sign-in request expired. Run the command again.")
