from __future__ import annotations

import errno
import inspect
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import RequirementsSnapshot


MAX_REQUIREMENTS_ARTIFACT_BYTES = 16 * 1024 * 1024
_CONTENT_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_OPEN_FLAGS = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FILE_OPEN_FLAGS = _FILE_OPEN_FLAGS | getattr(os, "O_NONBLOCK", 0)

_REQUIRED_OPEN_FLAGS = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
_REQUIRED_DIR_FD_FUNCTIONS = ("open", "stat", "unlink", "mkdir", "rename")


class RequirementsArtifactError(Exception):
    """Raised when a snapshot artifact or its storage boundary is unsafe."""


def _require_safe_filesystem_primitives() -> None:
    """Fail closed when this platform cannot enforce the storage boundary."""

    missing: list[str] = []
    for flag_name in _REQUIRED_OPEN_FLAGS:
        value = getattr(os, flag_name, None)
        if not isinstance(value, int) or value <= 0:
            missing.append(flag_name)

    supports_dir_fd = getattr(os, "supports_dir_fd", frozenset())
    for function_name in _REQUIRED_DIR_FD_FUNCTIONS:
        function = getattr(os, function_name, None)
        if not callable(function) or function not in supports_dir_fd:
            missing.append(f"{function_name}(dir_fd)")

    supports_follow_symlinks = getattr(os, "supports_follow_symlinks", frozenset())
    if os.stat not in supports_follow_symlinks:
        missing.append("stat(follow_symlinks=False)")

    replace = getattr(os, "replace", None)
    try:
        replace_parameters = inspect.signature(replace).parameters if callable(replace) else {}
    except (TypeError, ValueError):
        replace_parameters = {}
    if not {"src_dir_fd", "dst_dir_fd"}.issubset(replace_parameters):
        missing.append("replace(src_dir_fd,dst_dir_fd)")

    for function_name in (
        "fchmod",
        "fstat",
        "fsync",
        "geteuid",
        "lseek",
        "read",
        "write",
    ):
        if not callable(getattr(os, function_name, None)):
            missing.append(function_name)

    if missing:
        raise RequirementsArtifactError(
            "Secure requirements artifact storage is unavailable; missing filesystem "
            f"primitives: {', '.join(sorted(set(missing)))}"
        )


@dataclass(frozen=True)
class RequirementsArtifactPaths:
    content_hash: str
    current: Path
    historical: Path
    history_created: bool


def canonical_requirements_snapshot_document(
    snapshot: RequirementsSnapshot,
) -> dict[str, Any]:
    """Return the stable on-disk document represented by the content hash."""

    document = snapshot.canonical_content()
    document["content_hash"] = snapshot.calculate_content_hash()
    return document


def canonical_requirements_snapshot_json(snapshot: RequirementsSnapshot) -> str:
    return (
        json.dumps(
            canonical_requirements_snapshot_document(snapshot),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_requirements_snapshot_artifacts(
    workspace_path: Path,
    snapshot: RequirementsSnapshot,
) -> RequirementsArtifactPaths:
    """Write current and immutable snapshot files inside a private safe boundary."""

    _require_safe_filesystem_primitives()
    content_hash = snapshot.calculate_content_hash()
    if not _CONTENT_HASH_RE.fullmatch(content_hash):
        raise RequirementsArtifactError("Requirements snapshot produced an unsafe content hash")

    serialized = canonical_requirements_snapshot_json(snapshot).encode("utf-8")
    _validate_artifact_size(serialized, "requirements snapshot")
    symphony_dir = workspace_path / ".symphony"
    history_dir = symphony_dir / "requirements-snapshots"
    current_path = symphony_dir / "requirements-snapshot.json"
    historical_path = history_dir / f"{content_hash}.json"

    try:
        with _open_directory_path(workspace_path) as workspace_fd:
            with _open_private_directory_at(
                workspace_fd,
                ".symphony",
                symphony_dir,
                create=True,
            ) as symphony_fd:
                with _open_private_directory_at(
                    symphony_fd,
                    "requirements-snapshots",
                    history_dir,
                    create=True,
                ) as history_fd:
                    history_created = _write_history_once_at(
                        history_fd,
                        f"{content_hash}.json",
                        historical_path,
                        serialized,
                    )
                    _atomic_replace_at(
                        symphony_fd,
                        "requirements-snapshot.json",
                        current_path,
                        serialized,
                    )
                    _validate_installed_artifact_at(
                        history_fd, f"{content_hash}.json", historical_path, serialized
                    )
                    _validate_installed_artifact_at(
                        symphony_fd, "requirements-snapshot.json", current_path, serialized
                    )
    except RequirementsArtifactError:
        raise
    except OSError as exc:
        raise RequirementsArtifactError(
            f"Could not safely write requirements snapshots in {symphony_dir}: {exc}"
        ) from exc

    return RequirementsArtifactPaths(
        content_hash=content_hash,
        current=current_path,
        historical=historical_path,
        history_created=history_created,
    )


def read_requirements_snapshot_artifact(path: Path) -> RequirementsSnapshot:
    _require_safe_filesystem_primitives()
    try:
        absolute_path = Path(os.path.abspath(os.fspath(path)))
        with _open_snapshot_parent_path(absolute_path) as parent_fd:
            serialized = _read_regular_file_at(parent_fd, absolute_path.name, path)
        snapshot = RequirementsSnapshot.model_validate_json(serialized)
    except RequirementsArtifactError:
        raise
    except (OSError, ValueError) as exc:
        raise RequirementsArtifactError(f"Could not read requirements snapshot {path}: {exc}") from exc
    calculated = snapshot.calculate_content_hash()
    if snapshot.content_hash != calculated:
        raise RequirementsArtifactError(
            f"Requirements snapshot hash mismatch in {path}: "
            f"stored {snapshot.content_hash or 'missing'}, calculated {calculated}"
        )
    if (
        absolute_path.parent.name == "requirements-snapshots"
        and absolute_path.name != f"{calculated}.json"
    ):
        raise RequirementsArtifactError(
            f"Requirements snapshot history filename mismatch in {path}: "
            f"expected {calculated}.json"
        )
    return snapshot


@contextmanager
def _open_snapshot_parent_path(path: Path) -> Iterator[int]:
    """Open a parent and enforce its managed boundary when one is present."""

    parent = path.parent
    if parent.name == ".symphony":
        with _open_directory_path(parent.parent) as workspace_fd:
            with _open_private_directory_at(
                workspace_fd,
                ".symphony",
                parent,
                create=False,
            ) as symphony_fd:
                yield symphony_fd
        return
    if (
        parent.name == "requirements-snapshots"
        and parent.parent.name == ".symphony"
    ):
        symphony_dir = parent.parent
        with _open_directory_path(symphony_dir.parent) as workspace_fd:
            with _open_private_directory_at(
                workspace_fd,
                ".symphony",
                symphony_dir,
                create=False,
            ) as symphony_fd:
                with _open_private_directory_at(
                    symphony_fd,
                    "requirements-snapshots",
                    parent,
                    create=False,
                ) as history_fd:
                    yield history_fd
        return
    with _open_directory_path(parent) as parent_fd:
        yield parent_fd


@contextmanager
def _open_directory_path(path: Path) -> Iterator[int]:
    """Open every path component without following symlinks."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if not parts:
        raise RequirementsArtifactError(f"Invalid directory path: {path}")

    fd = -1
    try:
        fd = os.open(parts[0], _DIRECTORY_OPEN_FLAGS)
        for component in parts[1:]:
            next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        directory_stat = os.fstat(fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise RequirementsArtifactError(f"Artifact directory is not a directory: {path}")
        yield fd
    except RequirementsArtifactError:
        raise
    except OSError as exc:
        raise RequirementsArtifactError(
            f"Could not safely open artifact directory {path}: {exc}"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)


@contextmanager
def _open_private_directory_at(
    parent_fd: int,
    name: str,
    display_path: Path,
    *,
    create: bool,
) -> Iterator[int]:
    if name in {"", ".", ".."} or os.sep in name or (os.altsep and os.altsep in name):
        raise RequirementsArtifactError(f"Unsafe artifact directory name: {name!r}")
    if create:
        try:
            os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
            _fsync_directory(parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RequirementsArtifactError(
                f"Could not create private artifact directory {display_path}: {exc}"
            ) from exc

    fd = -1
    try:
        fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        directory_stat = os.fstat(fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise RequirementsArtifactError(
                f"Artifact path is not a real directory: {display_path}"
            )
        _require_current_owner(directory_stat, display_path)
        # Existing Symphony directories may predate this boundary. Once ownership is
        # established, make them private before creating or opening artifact files.
        if stat.S_IMODE(directory_stat.st_mode) != _DIRECTORY_MODE:
            os.fchmod(fd, _DIRECTORY_MODE)
            directory_stat = os.fstat(fd)
            if stat.S_IMODE(directory_stat.st_mode) != _DIRECTORY_MODE:
                raise RequirementsArtifactError(
                    f"Artifact directory is not private: {display_path}"
                )
        yield fd
    except RequirementsArtifactError:
        raise
    except OSError as exc:
        raise RequirementsArtifactError(
            f"Could not safely open artifact directory {display_path}: {exc}"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _write_history_once_at(
    directory_fd: int,
    name: str,
    display_path: Path,
    serialized: bytes,
) -> bool:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_OPEN_FLAGS
    fd = -1
    created = False
    try:
        fd = os.open(name, flags, _FILE_MODE, dir_fd=directory_fd)
        created = True
        _make_file_private(fd, display_path)
        _write_all(fd, serialized)
        os.fsync(fd)
        _require_directory_entry_matches_fd(
            directory_fd,
            name,
            fd,
            display_path,
        )
        _require_open_file_content(fd, serialized, display_path)
        _fsync_directory(directory_fd)
        _require_directory_entry_matches_fd(
            directory_fd,
            name,
            fd,
            display_path,
        )
        return True
    except FileExistsError:
        existing = _read_regular_file_at(directory_fd, name, display_path)
        if existing != serialized:
            raise RequirementsArtifactError(
                f"Immutable requirements snapshot history was modified: {display_path}"
            )
        return False
    except (RequirementsArtifactError, OSError) as exc:
        if created and fd >= 0:
            _unlink_if_entry_matches_fd(directory_fd, name, fd)
        if isinstance(exc, RequirementsArtifactError):
            raise
        raise RequirementsArtifactError(
            f"Could not write immutable requirements snapshot {display_path}: {exc}"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _atomic_replace_at(
    directory_fd: int,
    name: str,
    display_path: Path,
    serialized: bytes,
) -> None:
    _reject_unsafe_existing_target(directory_fd, name, display_path)
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    temporary_fd = -1
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_OPEN_FLAGS,
            _FILE_MODE,
            dir_fd=directory_fd,
        )
        _make_file_private(temporary_fd, display_path)
        _write_all(temporary_fd, serialized)
        os.fsync(temporary_fd)
        _require_directory_entry_matches_fd(
            directory_fd,
            temporary_name,
            temporary_fd,
            display_path,
        )
        _require_open_file_content(temporary_fd, serialized, display_path)
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        _require_directory_entry_matches_fd(
            directory_fd,
            name,
            temporary_fd,
            display_path,
        )
        _require_open_file_content(temporary_fd, serialized, display_path)
        _fsync_directory(directory_fd)
        _require_directory_entry_matches_fd(
            directory_fd,
            name,
            temporary_fd,
            display_path,
        )
        _require_open_file_content(temporary_fd, serialized, display_path)
    except RequirementsArtifactError:
        raise
    except OSError as exc:
        raise RequirementsArtifactError(
            f"Could not write current requirements snapshot {display_path}: {exc}"
        ) from exc
    finally:
        if temporary_fd >= 0:
            _unlink_if_entry_matches_fd(
                directory_fd,
                temporary_name,
                temporary_fd,
            )
            try:
                os.close(temporary_fd)
            except OSError:
                pass


def _reject_unsafe_existing_target(directory_fd: int, name: str, display_path: Path) -> None:
    try:
        target_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RequirementsArtifactError(
            f"Could not inspect current requirements snapshot {display_path}: {exc}"
        ) from exc
    _require_safe_regular_file(target_stat, display_path)


def _require_directory_entry_matches_fd(
    directory_fd: int,
    name: str,
    fd: int,
    display_path: Path,
) -> None:
    """Require a directory entry to still name the exact safe open file."""

    try:
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        open_stat = os.fstat(fd)
    except OSError as exc:
        raise RequirementsArtifactError(
            f"Requirements snapshot changed while being written: {display_path}: {exc}"
        ) from exc
    if not stat.S_ISREG(open_stat.st_mode):
        raise RequirementsArtifactError(
            f"Requirements snapshot changed while being written: {display_path}"
        )
    _require_current_owner(open_stat, display_path)
    if not stat.S_ISREG(entry_stat.st_mode) or (
        entry_stat.st_dev, entry_stat.st_ino
    ) != (open_stat.st_dev, open_stat.st_ino):
        raise RequirementsArtifactError(
            f"Requirements snapshot changed while being written: {display_path}"
        )
    _require_safe_regular_file(entry_stat, display_path)
    _require_safe_regular_file(open_stat, display_path)


def _unlink_if_entry_matches_fd(directory_fd: int, name: str, fd: int) -> None:
    """Remove only an entry that still names our open file."""

    try:
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        open_stat = os.fstat(fd)
        if (entry_stat.st_dev, entry_stat.st_ino) != (open_stat.st_dev, open_stat.st_ino):
            return
        if not stat.S_ISREG(entry_stat.st_mode):
            return
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        return


def _require_open_file_content(
    fd: int,
    expected: bytes,
    display_path: Path,
) -> None:
    """Validate bytes through a newly-created artifact's still-open descriptor."""

    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = MAX_REQUIREMENTS_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise RequirementsArtifactError(
            f"Could not verify requirements snapshot {display_path}: {exc}"
        ) from exc
    actual = b"".join(chunks)
    _validate_artifact_size(actual, str(display_path))
    if actual != expected:
        raise RequirementsArtifactError(
            f"Requirements snapshot changed while being written: {display_path}"
        )


def _validate_installed_artifact_at(
    directory_fd: int,
    name: str,
    display_path: Path,
    expected: bytes,
) -> None:
    actual = _read_regular_file_at(directory_fd, name, display_path)
    if actual != expected:
        raise RequirementsArtifactError(
            f"Installed requirements snapshot does not match canonical content: {display_path}"
        )


def _read_regular_file_at(directory_fd: int, name: str, display_path: Path) -> bytes:
    fd = -1
    try:
        fd = os.open(name, os.O_RDONLY | _READ_FILE_OPEN_FLAGS, dir_fd=directory_fd)
        file_stat = os.fstat(fd)
        _require_safe_regular_file(file_stat, display_path)
        if file_stat.st_size > MAX_REQUIREMENTS_ARTIFACT_BYTES:
            raise RequirementsArtifactError(
                f"Requirements snapshot exceeds {MAX_REQUIREMENTS_ARTIFACT_BYTES} bytes: {display_path}"
            )
        chunks: list[bytes] = []
        remaining = MAX_REQUIREMENTS_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        serialized = b"".join(chunks)
        _validate_artifact_size(serialized, str(display_path))
        return serialized
    except RequirementsArtifactError:
        raise
    except OSError as exc:
        raise RequirementsArtifactError(
            f"Could not safely read requirements snapshot {display_path}: {exc}"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _require_safe_regular_file(file_stat: os.stat_result, display_path: Path) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise RequirementsArtifactError(
            f"Requirements snapshot is not a regular file: {display_path}"
        )
    _require_current_owner(file_stat, display_path)
    if file_stat.st_nlink != 1:
        raise RequirementsArtifactError(
            f"Requirements snapshot has unsafe hard links: {display_path}"
        )


def _require_current_owner(file_stat: os.stat_result, display_path: Path) -> None:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and file_stat.st_uid != geteuid():
        raise RequirementsArtifactError(
            f"Artifact path is not owned by the current user: {display_path}"
        )


def _make_file_private(fd: int, display_path: Path) -> None:
    """Defensively enforce and verify private permissions on a newly created file."""

    os.fchmod(fd, _FILE_MODE)
    file_stat = os.fstat(fd)
    _require_safe_regular_file(file_stat, display_path)
    if stat.S_IMODE(file_stat.st_mode) != _FILE_MODE:
        raise RequirementsArtifactError(
            f"Requirements snapshot is not private: {display_path}"
        )


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    written = 0
    while written < len(view):
        try:
            count = os.write(fd, view[written:])
        except InterruptedError:
            continue
        if count <= 0:
            raise OSError(errno.EIO, "short write while storing requirements snapshot")
        written += count


def _validate_artifact_size(content: bytes, display_name: str) -> None:
    if len(content) > MAX_REQUIREMENTS_ARTIFACT_BYTES:
        raise RequirementsArtifactError(
            f"Requirements snapshot exceeds {MAX_REQUIREMENTS_ARTIFACT_BYTES} bytes: {display_name}"
        )


def _fsync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        # Some filesystems/platforms do not implement directory fsync.
        if exc.errno not in {errno.EBADF, errno.EINVAL, errno.ENOTSUP, errno.EROFS}:
            raise
