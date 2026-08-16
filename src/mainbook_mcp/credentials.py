"""Private per-user credential storage for local stdio clients."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

DEFAULT_API_BASE = "https://api.mainbook.ai"
KEYRING_SERVICE = "mainbook-mcp"
StorageKind = Literal["keyring", "file"]


class CredentialError(RuntimeError):
    """A sanitized local configuration or storage failure."""


@dataclass(frozen=True)
class StoredCredential:
    """Credential plus non-secret metadata used by status output."""

    api_base: str
    api_key: str
    client_name: str
    account: str | None
    created_at: str | None
    storage: StorageKind


@dataclass(frozen=True)
class DeleteResult:
    """What logout could prove about local deletion."""

    credential: StoredCredential | None
    removed_from: tuple[StorageKind, ...]
    keyring_unavailable: bool


def normalize_api_base(value: str) -> str:
    """Return one stable storage key while refusing remote cleartext HTTP."""
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise CredentialError(_api_base_error()) from exc
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in raw)
        or (scheme != "https" and not (scheme == "http" and hostname in {"localhost", "127.0.0.1"}))
    ):
        raise CredentialError(_api_base_error())

    displayed_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{displayed_host}:{port}" if port is not None else displayed_host
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def resolve_api_base(
    cli_value: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve flag, environment, and production default through one normalizer."""
    current_environ = os.environ if environ is None else environ
    configured = cli_value if cli_value is not None else current_environ.get(
        "MAINBOOK_API_BASE_URL", DEFAULT_API_BASE
    )
    return normalize_api_base(configured)


def credentials_path() -> Path:
    """Return the required fallback path without caching the user's home directory."""
    return Path.home() / ".config" / "mainbook" / "credentials.json"


def load_credential(api_base: str) -> StoredCredential | None:
    """Prefer a working OS keyring, then fall back silently to the private JSON file."""
    normalized = normalize_api_base(api_base)
    keyring = _load_keyring()
    if keyring is not None:
        try:
            raw = keyring.get_password(KEYRING_SERVICE, normalized)
        except Exception:
            raw = None
        stored = _credential_from_secret(raw, api_base=normalized, storage="keyring")
        if stored is not None:
            return stored
    return _load_file_credential(normalized)


def save_credential(
    api_base: str,
    *,
    api_key: str,
    client_name: str,
) -> StoredCredential:
    """Use keyring when it accepts a round trip; otherwise atomically write the fallback file."""
    normalized = normalize_api_base(api_base)
    clean_key = api_key.strip()
    clean_name = client_name.strip()
    if not clean_key or not clean_name:
        raise CredentialError("MainBook returned an incomplete credential; nothing was stored.")
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = {
        "api_key": clean_key,
        "client_name": clean_name,
        "account": None,
        "created_at": created_at,
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    keyring = _load_keyring()
    if keyring is not None:
        try:
            keyring.set_password(KEYRING_SERVICE, normalized, serialized)
            if keyring.get_password(KEYRING_SERVICE, normalized) == serialized:
                with suppress(CredentialError):
                    _delete_file_entry(normalized)
                return StoredCredential(
                    api_base=normalized,
                    api_key=clean_key,
                    client_name=clean_name,
                    account=None,
                    created_at=created_at,
                    storage="keyring",
                )
        except Exception:
            keyring = None
        if keyring is not None:
            try:
                keyring.delete_password(KEYRING_SERVICE, normalized)
            except Exception:
                keyring = None

    _save_file_entry(normalized, payload)
    return StoredCredential(
        api_base=normalized,
        api_key=clean_key,
        client_name=clean_name,
        account=None,
        created_at=created_at,
        storage="file",
    )


def delete_credential(api_base: str) -> DeleteResult:
    """Remove this API base from every reachable local store."""
    normalized = normalize_api_base(api_base)
    keyring = _load_keyring()
    keyring_credential: StoredCredential | None = None
    keyring_raw: str | None = None
    keyring_unavailable = False
    removed: list[StorageKind] = []

    if keyring is not None:
        try:
            keyring_raw = keyring.get_password(KEYRING_SERVICE, normalized)
            keyring_credential = _credential_from_secret(
                keyring_raw, api_base=normalized, storage="keyring"
            )
            if keyring_raw is not None:
                keyring.delete_password(KEYRING_SERVICE, normalized)
                removed.append("keyring")
        except Exception:
            keyring_unavailable = True

    file_credential = _load_file_credential(normalized)
    if _delete_file_entry(normalized):
        removed.append("file")
    return DeleteResult(
        credential=keyring_credential or file_credential,
        removed_from=tuple(removed),
        keyring_unavailable=keyring_unavailable,
    )


def _load_keyring() -> Any | None:
    try:
        return importlib.import_module("keyring")
    except (ImportError, RuntimeError):
        return None


def _credential_from_secret(
    raw: object,
    *,
    api_base: str,
    storage: StorageKind,
) -> StoredCredential | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return _credential_from_payload(payload, api_base=api_base, storage=storage)


def _credential_from_payload(
    payload: object,
    *,
    api_base: str,
    storage: StorageKind,
) -> StoredCredential | None:
    if not isinstance(payload, dict):
        return None
    api_key = payload.get("api_key")
    client_name = payload.get("client_name")
    if not isinstance(api_key, str) or not api_key.strip():
        return None
    if not isinstance(client_name, str) or not client_name.strip():
        client_name = "Unknown MainBook MCP client"
    account = payload.get("account")
    created_at = payload.get("created_at")
    return StoredCredential(
        api_base=api_base,
        api_key=api_key.strip(),
        client_name=client_name.strip(),
        account=account.strip() if isinstance(account, str) and account.strip() else None,
        created_at=created_at if isinstance(created_at, str) else None,
        storage=storage,
    )


def _read_file_payload() -> dict[str, object]:
    try:
        payload = json.loads(credentials_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_file_credential(api_base: str) -> StoredCredential | None:
    payload = _read_file_payload().get(api_base)
    return _credential_from_payload(payload, api_base=api_base, storage="file")


def _save_file_entry(api_base: str, credential: dict[str, object]) -> None:
    payload = _read_file_payload()
    payload[api_base] = credential
    _write_file_payload(payload)


def _delete_file_entry(api_base: str) -> bool:
    target = credentials_path()
    payload = _read_file_payload()
    if api_base not in payload:
        return False
    del payload[api_base]
    if payload:
        _write_file_payload(payload)
    else:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CredentialError("The local credential file could not be updated.") from exc
    return True


def _write_file_payload(payload: dict[str, object]) -> None:
    target = credentials_path()
    directory = target.parent
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise OSError("credential directory must not be a symlink")
        os.chmod(directory, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".credentials-", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        os.chmod(target, 0o600)
    except OSError as exc:
        raise CredentialError("The local credential file could not be saved.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()


def _api_base_error() -> str:
    return (
        "API base must be an HTTPS URL without credentials, query, or fragment; "
        "plain HTTP is allowed only for localhost or 127.0.0.1."
    )
