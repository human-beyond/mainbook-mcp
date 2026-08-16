"""User-visible auth command behavior, including the no-secret-output gate."""

from __future__ import annotations

import pytest

from mainbook_mcp import __main__
from mainbook_mcp.auth import AuthFlowError, DeviceToken
from mainbook_mcp.credentials import CredentialError, DeleteResult, StoredCredential

API_KEY = "mb_live_command_output_must_never_contain_this_value"
OLD_API_KEY = "mb_live_previous_device_key_never_print"
API_BASE = "https://api.mainbook.ai"


def stored(*, storage: str = "file", api_key: str = API_KEY) -> StoredCredential:
    return StoredCredential(
        api_base=API_BASE,
        api_key=api_key,
        client_name="Claude Code on test-machine",
        account=None,
        created_at="2026-08-16T12:00:00Z",
        storage=storage,
    )


async def credential_is_valid(*args, **kwargs) -> bool:
    return True


async def credential_is_revoked(*args, **kwargs) -> bool:
    return False


async def revoke_succeeds(*args, **kwargs) -> bool:
    return True


def test_flagless_auth_login_uses_api_origin_stores_result_and_never_prints_key(
    monkeypatch, capsys
) -> None:
    login_bases: list[str] = []

    async def perform_login(**kwargs) -> DeviceToken:
        login_bases.append(kwargs["api_base"])
        kwargs["write"]("Sign-in code: ABCD2345X")
        return DeviceToken(api_key=API_KEY, client_name="Claude Code on test-machine")

    saved: list[tuple[str, str, str]] = []

    def save_credential(api_base: str, *, api_key: str, client_name: str) -> StoredCredential:
        saved.append((api_base, api_key, client_name))
        return stored()

    monkeypatch.delenv("MAINBOOK_API_BASE_URL", raising=False)
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: None)
    monkeypatch.setattr(__main__, "perform_login", perform_login)
    monkeypatch.setattr(__main__, "save_credential", save_credential)

    __main__.main(["auth", "login", "--no-browser"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert login_bases == [API_BASE]
    assert saved == [(API_BASE, API_KEY, "Claude Code on test-machine")]
    assert "Claude Code on test-machine" in combined
    assert "credentials.json" in combined
    assert API_KEY not in combined


def test_auth_login_warns_that_environment_key_will_keep_winning(monkeypatch, capsys) -> None:
    async def perform_login(**kwargs) -> DeviceToken:
        return DeviceToken(api_key=API_KEY, client_name="Terminal")

    monkeypatch.setenv("MAINBOOK_API_KEY", OLD_API_KEY)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: None)
    monkeypatch.setattr(__main__, "perform_login", perform_login)
    monkeypatch.setattr(__main__, "save_credential", lambda *args, **kwargs: stored())

    __main__.main(["auth", "login", "--no-browser"])

    combined = capsys.readouterr().out
    assert "MAINBOOK_API_KEY" in combined
    assert "override" in combined.lower()
    assert API_KEY not in combined
    assert OLD_API_KEY not in combined


def test_relogin_revokes_previous_device_key_before_saving_new_one(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    async def perform_login(**kwargs) -> DeviceToken:
        events.append(("login", kwargs["api_base"]))
        return DeviceToken(api_key=API_KEY, client_name="New terminal")

    async def revoke_credential(*, api_base: str, api_key: str) -> bool:
        events.append(("revoke", api_key))
        return True

    def save_credential(api_base: str, *, api_key: str, client_name: str) -> StoredCredential:
        events.append(("save", api_key))
        return stored(api_key=api_key)

    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(
        __main__, "load_credential", lambda api_base: stored(api_key=OLD_API_KEY)
    )
    monkeypatch.setattr(__main__, "perform_login", perform_login)
    monkeypatch.setattr(__main__, "revoke_credential", revoke_credential)
    monkeypatch.setattr(__main__, "save_credential", save_credential)

    __main__.main(["auth", "login", "--no-browser"])

    assert events == [
        ("login", API_BASE),
        ("revoke", OLD_API_KEY),
        ("save", API_KEY),
    ]


def test_failed_save_best_effort_revokes_the_just_issued_key(monkeypatch, capsys) -> None:
    revoked: list[str] = []

    async def perform_login(**kwargs) -> DeviceToken:
        return DeviceToken(api_key=API_KEY, client_name="Terminal")

    async def revoke_credential(*, api_base: str, api_key: str) -> bool:
        revoked.append(api_key)
        return True

    def fail_save(*args, **kwargs):
        raise CredentialError("The local credential file could not be saved.")

    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: None)
    monkeypatch.setattr(__main__, "perform_login", perform_login)
    monkeypatch.setattr(__main__, "save_credential", fail_save)
    monkeypatch.setattr(__main__, "revoke_credential", revoke_credential)

    with pytest.raises(SystemExit) as raised:
        __main__.main(["auth", "login", "--no-browser"])

    assert raised.value.code == 1
    assert revoked == [API_KEY]
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert API_KEY not in combined


def test_auth_status_reports_source_client_and_server_validity_without_key(
    monkeypatch, capsys
) -> None:
    checked: list[tuple[str, str]] = []

    async def check_credential(*, api_base: str, api_key: str) -> bool:
        checked.append((api_base, api_key))
        return True

    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: stored(storage="keyring"))
    monkeypatch.setattr(__main__, "check_credential", check_credential)

    __main__.main(["auth", "status"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert checked == [(API_BASE, API_KEY)]
    assert "signed in" in combined.lower()
    assert "valid server-side: yes" in combined.lower()
    assert "keyring" in combined.lower()
    assert "Claude Code on test-machine" in combined
    assert "not provided" in combined.lower()
    assert API_KEY not in combined


def test_auth_status_reports_revoked_stored_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: stored())
    monkeypatch.setattr(__main__, "check_credential", credential_is_revoked)

    __main__.main(["auth", "status"])

    combined = capsys.readouterr().out
    assert "Signed in: no" in combined
    assert "Valid server-side: no" in combined
    assert "revoked or invalid" in combined.lower()
    assert API_KEY not in combined


def test_auth_status_environment_wins_without_reading_storage(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setattr(__main__, "check_credential", credential_is_valid)
    monkeypatch.setattr(
        __main__,
        "load_credential",
        lambda api_base: (_ for _ in ()).throw(AssertionError("storage was read")),
    )

    __main__.main(["auth", "status"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "MAINBOOK_API_KEY" in combined
    assert "Valid server-side: yes" in combined
    assert API_KEY not in combined


def test_auth_logout_revokes_server_side_before_deleting_locally(monkeypatch, capsys) -> None:
    events: list[str] = []

    async def revoke_credential(*, api_base: str, api_key: str) -> bool:
        events.append("revoke")
        return True

    def delete_credential(api_base: str) -> DeleteResult:
        events.append("delete")
        return DeleteResult(
            credential=stored(), removed_from=("file",), keyring_unavailable=False
        )

    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: stored())
    monkeypatch.setattr(__main__, "revoke_credential", revoke_credential)
    monkeypatch.setattr(__main__, "delete_credential", delete_credential)

    __main__.main(["auth", "logout"])

    combined = capsys.readouterr().out
    assert events == ["revoke", "delete"]
    assert "Revoked the credential server-side" in combined
    assert "Removed the local credential" in combined
    assert API_KEY not in combined


def test_logout_deletes_locally_and_warns_plainly_when_revocation_fails(
    monkeypatch, capsys
) -> None:
    deleted: list[str] = []

    async def revoke_fails(*args, **kwargs) -> bool:
        raise AuthFlowError("Could not reach MainBook to revoke the credential.")

    def delete_credential(api_base: str) -> DeleteResult:
        deleted.append(api_base)
        return DeleteResult(
            credential=stored(), removed_from=("file",), keyring_unavailable=False
        )

    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: stored())
    monkeypatch.setattr(__main__, "revoke_credential", revoke_fails)
    monkeypatch.setattr(__main__, "delete_credential", delete_credential)

    __main__.main(["auth", "logout"])

    combined = capsys.readouterr().out
    assert deleted == [API_BASE]
    assert "Server-side revocation failed" in combined
    assert "may still be active" in combined
    assert "Removed the local credential" in combined
    assert API_KEY not in combined


def test_auth_commands_reject_non_https_remote_base(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        __main__.main(["auth", "status", "--api-base", "http://staging.example.com"])

    assert raised.value.code != 0
    captured = capsys.readouterr()
    assert "HTTPS" in captured.err


def test_auth_uses_normalized_environment_api_base(monkeypatch, capsys) -> None:
    loaded: list[str] = []
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setenv("MAINBOOK_API_BASE_URL", "https://staging.example.com/")
    monkeypatch.setattr(
        __main__, "load_credential", lambda api_base: loaded.append(api_base) or None
    )

    __main__.main(["auth", "status"])

    assert loaded == ["https://staging.example.com"]
    assert "Signed in: no" in capsys.readouterr().out


def test_auth_status_without_credential_points_to_login(monkeypatch, capsys) -> None:
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: None)

    __main__.main(["auth", "status"])

    captured = capsys.readouterr()
    assert "Signed in: no" in captured.out
    assert "mainbook-mcp auth login" in captured.out


def test_auth_logout_reports_locked_keyring_and_environment_override(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: stored(storage="keyring"))
    monkeypatch.setattr(__main__, "revoke_credential", revoke_succeeds)
    monkeypatch.setattr(
        __main__,
        "delete_credential",
        lambda api_base: DeleteResult(
            credential=stored(storage="keyring"),
            removed_from=(),
            keyring_unavailable=True,
        ),
    )

    __main__.main(["auth", "logout"])

    captured = capsys.readouterr()
    assert "could not be removed" in captured.out
    assert "could not be checked or deleted" in captured.out
    assert "MAINBOOK_API_KEY is still set" in captured.out
    assert API_KEY not in captured.out + captured.err


def test_auth_logout_with_nothing_local_does_not_invent_server_state(monkeypatch, capsys) -> None:
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: None)
    monkeypatch.setattr(
        __main__,
        "delete_credential",
        lambda api_base: DeleteResult(
            credential=None,
            removed_from=(),
            keyring_unavailable=False,
        ),
    )

    __main__.main(["auth", "logout"])

    captured = capsys.readouterr()
    assert "No stored credential" in captured.out
    assert "server-side" not in captured.out.lower()
