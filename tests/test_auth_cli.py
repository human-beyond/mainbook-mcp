"""User-visible auth command behavior, including the no-secret-output gate."""

from __future__ import annotations

from mainbook_mcp import __main__
from mainbook_mcp.auth import DeviceToken
from mainbook_mcp.credentials import DeleteResult, StoredCredential

API_KEY = "mb_live_command_output_must_never_contain_this_value"
API_BASE = "https://mainbook.ai"


def stored(*, storage: str = "file") -> StoredCredential:
    return StoredCredential(
        api_base=API_BASE,
        api_key=API_KEY,
        client_name="Claude Code on test-machine",
        account=None,
        created_at="2026-08-16T12:00:00Z",
        storage=storage,
    )


def test_auth_login_stores_result_and_never_prints_key(monkeypatch, capsys) -> None:
    async def perform_login(**kwargs) -> DeviceToken:
        kwargs["write"]("Sign-in code: ABCD2345X")
        return DeviceToken(api_key=API_KEY, client_name="Claude Code on test-machine")

    saved: list[tuple[str, str, str]] = []

    def save_credential(api_base: str, *, api_key: str, client_name: str) -> StoredCredential:
        saved.append((api_base, api_key, client_name))
        return stored()

    monkeypatch.setattr(__main__, "perform_login", perform_login)
    monkeypatch.setattr(__main__, "save_credential", save_credential)

    __main__.main(["auth", "login", "--no-browser"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert saved == [(API_BASE, API_KEY, "Claude Code on test-machine")]
    assert "Claude Code on test-machine" in combined
    assert "credentials.json" in combined
    assert API_KEY not in combined


def test_auth_status_reports_source_client_and_unavailable_account_without_key(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: stored(storage="keyring"))

    __main__.main(["auth", "status"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "signed in" in combined.lower()
    assert "keyring" in combined.lower()
    assert "Claude Code on test-machine" in combined
    assert "not provided" in combined.lower()
    assert API_KEY not in combined


def test_auth_status_environment_wins_without_reading_storage(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
    monkeypatch.setattr(
        __main__,
        "load_credential",
        lambda api_base: (_ for _ in ()).throw(AssertionError("storage was read")),
    )

    __main__.main(["auth", "status"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "MAINBOOK_API_KEY" in combined
    assert API_KEY not in combined


def test_auth_logout_is_honest_about_missing_server_revocation_and_never_prints_key(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        __main__,
        "delete_credential",
        lambda api_base: DeleteResult(
            credential=stored(), removed_from=("file",), keyring_unavailable=False
        ),
    )

    __main__.main(["auth", "logout"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "removed" in combined.lower()
    assert "still valid server-side" in combined.lower()
    assert "https://mainbook.ai/developer" in combined
    assert API_KEY not in combined


def test_auth_commands_reject_non_https_remote_base(capsys) -> None:
    try:
        __main__.main(["auth", "status", "--api-base", "http://staging.example.com"])
    except SystemExit as exc:
        assert exc.code != 0
    else:  # pragma: no cover - argparse must terminate invalid input
        raise AssertionError("unsafe base was accepted")

    captured = capsys.readouterr()
    assert "HTTPS" in captured.err


def test_auth_status_without_credential_points_to_login(monkeypatch, capsys) -> None:
    monkeypatch.delenv("MAINBOOK_API_KEY", raising=False)
    monkeypatch.setattr(__main__, "load_credential", lambda api_base: None)

    __main__.main(["auth", "status"])

    captured = capsys.readouterr()
    assert "Signed in: no" in captured.out
    assert "mainbook-mcp auth login" in captured.out


def test_auth_logout_reports_locked_keyring_and_environment_override(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAINBOOK_API_KEY", API_KEY)
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
