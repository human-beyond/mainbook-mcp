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


def test_server_json_version_matches_pyproject() -> None:
    import json

    root = pathlib.Path(__file__).resolve().parent.parent
    server = json.loads((root / "server.json").read_text())
    expected = _pyproject_version()
    assert server["version"] == expected
    assert server["packages"][0]["version"] == expected


def test_bundle_and_cursor_manifests_match_pyproject() -> None:
    import json

    root = pathlib.Path(__file__).resolve().parent.parent
    expected = _pyproject_version()
    assert json.loads((root / "manifest.json").read_text())["version"] == expected
    plugin = json.loads((root / ".cursor-plugin" / "plugin.json").read_text())
    assert plugin["version"] == expected
