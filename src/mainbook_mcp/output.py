"""Safe local result delivery and persistent output-folder preferences."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .errors import MainBookFileError
from .files import _is_within_allowed_root
from .models import ResultType

NEXT_TO_SOURCE = "next_to_source"


@dataclass(frozen=True)
class OutputDestination:
    """A checked, absolute base path plus the user-visible placement explanation."""

    path: Path
    reason: str
    warning: str | None = None


@dataclass(frozen=True)
class PreferenceState:
    """The effective preference and whether a stored path had to be ignored."""

    folder: Path | None
    ignored: bool = False


def preferences_path() -> Path:
    """Return the per-user settings path without caching HOME across client calls."""
    return Path.home() / ".mainbook" / "preferences.json"


def read_output_preference(allowed_roots: tuple[Path, ...]) -> PreferenceState:
    """Read a valid allowed folder; malformed or unreadable JSON behaves as no setting."""
    try:
        payload = json.loads(preferences_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return PreferenceState(folder=None)
    if not isinstance(payload, dict):
        return PreferenceState(folder=None)
    value = payload.get("output_folder")
    if value == NEXT_TO_SOURCE:
        return PreferenceState(folder=None)
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return PreferenceState(folder=None)
    try:
        folder = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return PreferenceState(folder=None, ignored=True)
    if not folder.is_dir() or not _folder_is_allowed(folder, allowed_roots):
        return PreferenceState(folder=None, ignored=True)
    return PreferenceState(folder=folder)


def write_output_preference(value: str) -> None:
    """Atomically replace preferences.json with private directory and file modes."""
    target = preferences_path()
    directory = target.parent
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".preferences-", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump({"output_folder": value}, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        os.chmod(target, 0o600)
    except OSError as exc:
        raise MainBookFileError("The output-folder preference could not be saved.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()


def validate_preference_folder(path: str, allowed_roots: tuple[Path, ...]) -> Path:
    """Resolve and allowlist a directory before it is persisted as a preference."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise MainBookFileError("The default output folder must be an absolute path.")
    try:
        folder = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MainBookFileError("The default output folder does not exist or cannot be read.") from exc
    if not folder.is_dir():
        raise MainBookFileError("The default output folder must be an existing directory.")
    if not _folder_is_allowed(folder, allowed_roots):
        raise _outside_allowed_roots(allowed_roots)
    return folder


def prepare_output_path(
    requested_path: str | Path,
    *,
    result_type: ResultType,
    default_filename: str,
    allowed_roots: tuple[Path, ...],
) -> Path:
    """Resolve the real parent and return an allowlisted absolute base output path."""
    requested = Path(requested_path).expanduser()
    if not requested.is_absolute():
        raise MainBookFileError("output_path must be an absolute file or folder path.")

    if requested.is_dir():
        try:
            parent = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MainBookFileError("The output folder does not exist or cannot be accessed.") from exc
        filename = _filename_with_result_extension(default_filename, result_type)
    else:
        try:
            parent = requested.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MainBookFileError(
                "The parent folder for output_path does not exist or cannot be accessed."
            ) from exc
        filename = _filename_with_result_extension(requested.name, result_type)

    target = parent / filename
    if not _is_within_allowed_root(target, allowed_roots):
        raise _outside_allowed_roots(allowed_roots)
    return target


def _open_checked_parent(parent: Path, allowed_roots: tuple[Path, ...]) -> int | None:
    """Pin the already checked folder itself, so a later swap cannot redirect the write.

    ``prepare_output_path`` resolves the parent and checks it against the allowed roots, but
    between that check and the ``open`` the folder can be replaced — with a symlink pointing
    anywhere — and the result then lands outside the allowlist. Holding a descriptor on the
    directory means every later step addresses the directory we checked, not a path that can be
    re-pointed underneath us. ``O_NOFOLLOW`` is safe here because the parent is already resolved,
    so its last component is a real directory rather than a symlink.
    """
    if not allowed_roots or os.open not in os.supports_dir_fd:
        return None  # Windows offers no dir_fd; the plain path write below still applies O_EXCL.
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError as exc:
        raise MainBookFileError(
            "The output folder changed while the result was being written."
        ) from exc
    try:
        pinned = os.fstat(descriptor)
        if not stat.S_ISDIR(pinned.st_mode):
            raise MainBookFileError("The output folder is no longer a folder.")
        current = parent.resolve(strict=True)
        if (current.stat().st_dev, current.stat().st_ino) != (pinned.st_dev, pinned.st_ino):
            raise MainBookFileError("The output folder changed while the result was being written.")
        if not _is_within_allowed_root(current / "probe", allowed_roots):
            raise _outside_allowed_roots(allowed_roots)
    except OSError as exc:
        os.close(descriptor)
        raise MainBookFileError("The output folder could not be checked.") from exc
    except MainBookFileError:
        os.close(descriptor)
        raise
    return descriptor


def write_result_bytes(
    base_path: Path, data: bytes, allowed_roots: tuple[Path, ...] = ()
) -> Path:
    """Create a private result file without ever replacing an existing directory entry."""
    parent_fd = _open_checked_parent(base_path.parent, allowed_roots)
    try:
        return _write_into(base_path, data, parent_fd)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _write_into(base_path: Path, data: bytes, parent_fd: int | None) -> Path:
    for collision_index in range(1, 100_001):
        candidate = _collision_candidate(base_path, collision_index)
        descriptor: int | None = None
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            if parent_fd is None:
                descriptor = os.open(candidate, create_flags, 0o600)
            else:
                descriptor = os.open(candidate.name, create_flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise MainBookFileError("The result file could not be created.") from exc

        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                if parent_fd is None:
                    candidate.unlink()
                else:
                    os.unlink(candidate.name, dir_fd=parent_fd)
            raise MainBookFileError("The result file could not be written.") from exc
        return candidate
    raise MainBookFileError("Too many files already use the requested result name.")


def serialized_json_bytes(payload: object) -> bytes:
    """Serialize the already validated inline JSON export for optional local delivery."""
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()


def _filename_with_result_extension(filename: str, result_type: ResultType) -> str:
    if not filename or filename in {".", ".."}:
        raise MainBookFileError("output_path must include a valid file or existing folder.")
    return f"{Path(filename).stem}.{result_type}"


def _collision_candidate(base_path: Path, collision_index: int) -> Path:
    if collision_index == 1:
        return base_path
    return base_path.with_name(f"{base_path.stem} ({collision_index}){base_path.suffix}")


def _folder_is_allowed(folder: Path, allowed_roots: tuple[Path, ...]) -> bool:
    return _is_within_allowed_root(folder / ".mainbook-write-check", allowed_roots)


def _outside_allowed_roots(allowed_roots: tuple[Path, ...]) -> MainBookFileError:
    displayed = ", ".join(str(root) for root in allowed_roots) or "(none active)"
    return MainBookFileError("Results may only be written inside the allowed folders: " + displayed)
