"""The version the server reports must be the version we ship.

0.4.1 went to PyPI with ``__version__`` still reading 0.4.0, so the hosted
server introduced itself as 0.4.0 while the registry and PyPI said 0.4.1, and
the User-Agent we send to the MainBook API named the wrong release. Nothing
tied the two numbers together, so the drift was silent.
"""

from __future__ import annotations

import pathlib
import tomllib

from mainbook_mcp import __version__


def _pyproject_version() -> str:
    root = pathlib.Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def test_package_version_matches_pyproject() -> None:
    assert __version__ == _pyproject_version()


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_server_json_version_matches_pyproject() -> None:
    """The shipped package pins one number; the registry entry may run ahead.

    Registry versions are immutable, so a metadata-only correction (0.5.2 on
    2026-08-20 removed the required ``Authorization`` header from the hosted
    remote) needs its own registry version even though no code changed. What
    must never drift is the package the entry points at.
    """

    import json

    root = pathlib.Path(__file__).resolve().parent.parent
    server = json.loads((root / "server.json").read_text())
    expected = _pyproject_version()
    assert server["packages"][0]["version"] == expected
    assert _as_tuple(server["version"]) >= _as_tuple(expected)


def test_bundle_and_cursor_manifests_match_pyproject() -> None:
    import json

    root = pathlib.Path(__file__).resolve().parent.parent
    expected = _pyproject_version()
    assert json.loads((root / "manifest.json").read_text())["version"] == expected
    plugin = json.loads((root / ".cursor-plugin" / "plugin.json").read_text())
    assert plugin["version"] == expected
