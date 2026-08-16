"""Credential-storage contract for terminal sign-in."""

from __future__ import annotations

import json
import stat

import pytest

from mainbook_mcp import credentials

API_BASE = "https://mainbook.ai"
API_KEY = "mb_live_cli_storage_secret"


class MemoryKeyring:
    def __init__(self, *, locked: bool = False) -> None:
        self.locked = locked
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        if self.locked:
            raise RuntimeError("locked")
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.locked:
            raise RuntimeError("locked")
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.locked:
            raise RuntimeError("locked")
        self.values.pop((service, username), None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://mainbook.ai", "https://mainbook.ai"),
        ("https://mainbook.ai/", "https://mainbook.ai"),
        ("http://localhost:8000/", "http://localhost:8000"),
        ("http://127.0.0.1:9000", "http://127.0.0.1:9000"),
    ],
)
def test_api_base_accepts_https_and_explicit_local_http(value: str, expected: str) -> None:
    assert credentials.normalize_api_base(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://mainbook.ai",
        "http://staging.mainbook.ai",
        "ftp://mainbook.ai",
        "mainbook.ai",
        "https://user:secret@mainbook.ai",
        "https://mainbook.ai?query=1",
        "https://mainbook.ai#fragment",
    ],
)
def test_api_base_rejects_unsafe_or_ambiguous_values(value: str) -> None:
    with pytest.raises(credentials.CredentialError, match="HTTPS"):
        credentials.normalize_api_base(value)


def test_working_keyring_is_preferred_and_file_is_not_created(monkeypatch, tmp_path) -> None:
    keyring = MemoryKeyring()
    target = tmp_path / "mainbook" / "credentials.json"
    monkeypatch.setattr(credentials, "_load_keyring", lambda: keyring)
    monkeypatch.setattr(credentials, "credentials_path", lambda: target)

    saved = credentials.save_credential(
        API_BASE,
        api_key=API_KEY,
        client_name="Claude Code on test-mac",
    )
    loaded = credentials.load_credential(API_BASE)

    assert saved.storage == "keyring"
    assert loaded is not None
    assert loaded.api_key == API_KEY
    assert loaded.client_name == "Claude Code on test-mac"
    assert loaded.account is None
    assert loaded.storage == "keyring"
    assert not target.exists()


def test_locked_keyring_falls_back_to_private_forward_compatible_file(
    monkeypatch, tmp_path
) -> None:
    target = tmp_path / "mainbook" / "credentials.json"
    monkeypatch.setattr(credentials, "_load_keyring", lambda: MemoryKeyring(locked=True))
    monkeypatch.setattr(credentials, "credentials_path", lambda: target)

    saved = credentials.save_credential(
        API_BASE,
        api_key=API_KEY,
        client_name="MainBook MCP on test-host",
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    loaded = credentials.load_credential(API_BASE)

    assert saved.storage == "file"
    assert payload[API_BASE]["api_key"] == API_KEY
    assert payload[API_BASE]["client_name"] == "MainBook MCP on test-host"
    assert isinstance(payload[API_BASE], dict)
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert loaded is not None
    assert loaded.api_key == API_KEY
    assert loaded.storage == "file"


def test_file_store_preserves_other_api_bases_and_deletes_only_requested_one(
    monkeypatch, tmp_path
) -> None:
    target = tmp_path / "mainbook" / "credentials.json"
    monkeypatch.setattr(credentials, "_load_keyring", lambda: None)
    monkeypatch.setattr(credentials, "credentials_path", lambda: target)
    local_base = "http://localhost:8000"
    credentials.save_credential(API_BASE, api_key=API_KEY, client_name="Production client")
    credentials.save_credential(
        local_base,
        api_key="mb_live_local_only",
        client_name="Local client",
    )

    result = credentials.delete_credential(API_BASE)
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert result.credential is not None
    assert result.credential.api_key == API_KEY
    assert result.removed_from == ("file",)
    assert API_BASE not in payload
    assert payload[local_base]["api_key"] == "mb_live_local_only"


def test_unknown_future_fields_do_not_break_loading(monkeypatch, tmp_path) -> None:
    target = tmp_path / "mainbook" / "credentials.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                API_BASE: {
                    "api_key": API_KEY,
                    "client_name": "Future client",
                    "account": "person@example.com",
                    "future_field": {"nested": True},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(credentials, "_load_keyring", lambda: None)
    monkeypatch.setattr(credentials, "credentials_path", lambda: target)

    loaded = credentials.load_credential(API_BASE)

    assert loaded is not None
    assert loaded.account == "person@example.com"
    assert loaded.api_key == API_KEY


def test_incomplete_credential_is_never_stored(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(credentials, "_load_keyring", lambda: None)
    monkeypatch.setattr(
        credentials, "credentials_path", lambda: tmp_path / "mainbook" / "credentials.json"
    )

    with pytest.raises(credentials.CredentialError, match="incomplete"):
        credentials.save_credential(API_BASE, api_key="", client_name="Terminal")


def test_working_keyring_credential_is_deleted(monkeypatch, tmp_path) -> None:
    keyring = MemoryKeyring()
    monkeypatch.setattr(credentials, "_load_keyring", lambda: keyring)
    monkeypatch.setattr(
        credentials, "credentials_path", lambda: tmp_path / "mainbook" / "credentials.json"
    )
    credentials.save_credential(API_BASE, api_key=API_KEY, client_name="Terminal")

    result = credentials.delete_credential(API_BASE)

    assert result.credential is not None
    assert result.credential.api_key == API_KEY
    assert result.removed_from == ("keyring",)
    assert credentials.load_credential(API_BASE) is None


def test_locked_keyring_is_reported_while_file_fallback_is_deleted(monkeypatch, tmp_path) -> None:
    target = tmp_path / "mainbook" / "credentials.json"
    keyring = MemoryKeyring(locked=True)
    monkeypatch.setattr(credentials, "_load_keyring", lambda: keyring)
    monkeypatch.setattr(credentials, "credentials_path", lambda: target)
    credentials.save_credential(API_BASE, api_key=API_KEY, client_name="Terminal")

    result = credentials.delete_credential(API_BASE)

    assert result.credential is not None
    assert result.removed_from == ("file",)
    assert result.keyring_unavailable is True
    assert not target.exists()


def test_missing_keyring_import_is_normal_optional_fallback(monkeypatch) -> None:
    def missing(name: str):
        raise ImportError(name)

    monkeypatch.setattr(credentials.importlib, "import_module", missing)

    assert credentials._load_keyring() is None


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps([]),
        json.dumps({"client_name": "missing key"}),
        json.dumps({"api_key": API_KEY, "client_name": ""}),
    ],
)
def test_invalid_or_old_keyring_values_are_handled_without_exposure(
    monkeypatch, tmp_path, payload: str
) -> None:
    keyring = MemoryKeyring()
    keyring.values[(credentials.KEYRING_SERVICE, API_BASE)] = payload
    monkeypatch.setattr(credentials, "_load_keyring", lambda: keyring)
    monkeypatch.setattr(
        credentials, "credentials_path", lambda: tmp_path / "mainbook" / "credentials.json"
    )

    loaded = credentials.load_credential(API_BASE)

    if "api_key" in payload and "missing key" not in payload:
        assert loaded is not None
        assert loaded.client_name == "Unknown MainBook MCP client"
    else:
        assert loaded is None
