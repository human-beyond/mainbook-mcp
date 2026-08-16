"""Single command-line entry point for stdio and Streamable HTTP transports."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .auth import AuthFlowError, detect_client_name, perform_login
from .credentials import (
    DEFAULT_API_BASE,
    CredentialError,
    StoredCredential,
    credentials_path,
    delete_credential,
    load_credential,
    normalize_api_base,
    save_credential,
)
from .files import normalize_allowed_roots
from .server import create_server

DEFAULT_ALLOWED_DIRS = ("~/Downloads", "~/Desktop", "~/Documents")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mainbook-mcp",
        description="Run the MainBook MCP server (or use 'mainbook-mcp auth --help')",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.getenv("MAINBOOK_MCP_TRANSPORT", "stdio"),
        help="stdio for local clients, or http for stateless Streamable HTTP",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MAINBOOK_MCP_HOST", "127.0.0.1"),
        help="HTTP bind host (ignored for stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MAINBOOK_MCP_PORT", "8000")),
        help="HTTP bind port (ignored for stdio)",
    )
    parser.add_argument(
        "allowed_dirs",
        nargs="*",
        metavar="DIR",
        help=(
            "folders allowed for local file_path access; overrides MAINBOOK_ALLOWED_DIRS and "
            "defaults"
        ),
    )
    return parser


def build_auth_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mainbook-mcp auth", description="Manage terminal sign-in"
    )
    commands = parser.add_subparsers(dest="auth_command", required=True)
    login = commands.add_parser("login", help="sign in through the browser")
    login.add_argument("--api-base", type=_api_base_argument, default=DEFAULT_API_BASE)
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="print the verification URL instead of opening it",
    )
    status = commands.add_parser("status", help="show whether and where a credential is configured")
    status.add_argument("--api-base", type=_api_base_argument, default=DEFAULT_API_BASE)
    logout = commands.add_parser("logout", help="remove the locally stored credential")
    logout.add_argument("--api-base", type=_api_base_argument, default=DEFAULT_API_BASE)
    return parser


def resolve_allowed_roots(
    cli_dirs: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Select exactly one configuration source, then return its existing directories."""
    current_environ = os.environ if environ is None else environ
    if cli_dirs:
        candidates = cli_dirs
    elif "MAINBOOK_ALLOWED_DIRS" in current_environ:
        candidates = current_environ["MAINBOOK_ALLOWED_DIRS"].split(os.pathsep)
    else:
        candidates = DEFAULT_ALLOWED_DIRS
    return normalize_allowed_roots(candidates)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "auth":
        _run_auth(arguments[1:])
        return
    args = build_parser().parse_args(arguments)
    allowed_roots = resolve_allowed_roots(args.allowed_dirs)
    displayed_roots = ", ".join(str(root) for root in allowed_roots) or "(none active)"
    print(f"MainBook local-file allowed folders: {displayed_roots}", file=sys.stderr)
    server = create_server(transport=args.transport, allowed_roots=allowed_roots)
    if args.transport == "stdio":
        server.run("stdio")
        return
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        stateless_http=True,
        json_response=True,
    )


def _run_auth(argv: Sequence[str]) -> None:
    parser = build_auth_parser()
    args = parser.parse_args(argv)
    api_base = args.api_base
    try:
        if args.auth_command == "login":
            token = asyncio.run(
                perform_login(
                    api_base=api_base,
                    client_name=detect_client_name(),
                    no_browser=args.no_browser,
                    write=print,
                )
            )
            stored = save_credential(
                api_base,
                api_key=token.api_key,
                client_name=token.client_name,
            )
            print(f"Signed in for {token.client_name}.")
            print(f"Credential stored in {_storage_description(stored)}.")
            return
        if args.auth_command == "status":
            _print_auth_status(api_base)
            return
        if args.auth_command == "logout":
            _logout(api_base)
            return
    except (AuthFlowError, CredentialError) as exc:
        parser.exit(1, f"mainbook-mcp auth: {exc}\n")


def _print_auth_status(api_base: str) -> None:
    if os.getenv("MAINBOOK_API_KEY", "").strip():
        print("Signed in: yes")
        print(f"API base: {api_base}")
        print("Credential source: MAINBOOK_API_KEY environment variable (takes precedence)")
        print("Account: not provided by environment-based authentication")
        return
    stored = load_credential(api_base)
    if stored is None:
        print("Signed in: no")
        print(f"API base: {api_base}")
        print("Run 'mainbook-mcp auth login' to sign in.")
        return
    print("Signed in: yes")
    print(f"API base: {stored.api_base}")
    print(f"Credential source: {_storage_description(stored)}")
    print(f"Client: {stored.client_name}")
    account = stored.account or "not provided by the device authorization backend"
    print(f"Account: {account}")


def _logout(api_base: str) -> None:
    result = delete_credential(api_base)
    if result.credential is None:
        if result.removed_from:
            print(f"Removed an unreadable local credential from {', '.join(result.removed_from)}.")
        else:
            print(f"No stored credential was found for {api_base}.")
    elif result.removed_from:
        sources = ", ".join(result.removed_from)
        print(f"Removed the local credential from {sources} for {result.credential.client_name}.")
    else:
        print(f"A local credential for {result.credential.client_name} could not be removed.")
    if result.keyring_unavailable:
        print("The OS keyring was unavailable, so its credential could not be checked or deleted.")
    environment_key_present = bool(os.getenv("MAINBOOK_API_KEY", "").strip())
    if environment_key_present:
        print(
            "MAINBOOK_API_KEY is still set in this environment and cannot be removed by this command."
        )
    if result.credential is not None or result.removed_from or environment_key_present:
        print(
            "Server-side revocation is not available in the implemented backend contract; "
            "treat the key as still valid server-side. Revoke it at https://mainbook.ai/developer."
        )


def _storage_description(stored: StoredCredential) -> str:
    if stored.storage == "keyring":
        return "the OS keyring (service mainbook-mcp)"
    return str(credentials_path())


def _api_base_argument(value: str) -> str:
    try:
        return normalize_api_base(value)
    except CredentialError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


if __name__ == "__main__":  # pragma: no cover - exercised through the console entry point
    main()
