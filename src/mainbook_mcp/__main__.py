"""Single command-line entry point for stdio and Streamable HTTP transports."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .files import normalize_allowed_roots
from .server import create_server

DEFAULT_ALLOWED_DIRS = ("~/Downloads", "~/Desktop", "~/Documents")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mainbook-mcp", description="Run the MainBook MCP server")
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
    args = build_parser().parse_args(argv)
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


if __name__ == "__main__":  # pragma: no cover - exercised through the console entry point
    main()
