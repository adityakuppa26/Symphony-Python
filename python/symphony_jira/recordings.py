"""Bounded, integrity-checked browser recordings owned by a verification run."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import BinaryIO, Iterator, Literal, Mapping, Sequence
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

MAX_RECORDINGS = 20
WEBM_SIGNATURE = b"\x1a\x45\xdf\xa3"
MAX_RECORDING_BYTES = 500 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
_RECORDING_PATH = re.compile(r"\.symphony/recordings/([0-9a-f]{32})/([0-9a-f]{32})\.webm")
_CAPTURE_PATH = re.compile(r"\.symphony/recordings/[0-9a-f]{32}")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_CHUNK_BYTES = 1024 * 1024


class RecordingError(ValueError):
    """Recording metadata or bytes cannot be safely used as verification evidence."""


class Recording(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    path: str = Field(max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(gt=0, le=MAX_RECORDING_BYTES)
    title: str = Field(min_length=1, max_length=500)
    spec: str = Field(max_length=1024)
    outcome: Literal["passed", "failed", "timed_out", "skipped", "unknown"]
    recorded_at: str = Field(max_length=64)

    @field_validator("spec")
    @classmethod
    def valid_spec(cls, value: str) -> str:
        path = Path(value)
        if (not re.fullmatch(r"playwright/integrated/[A-Za-z0-9_./-]+\.spec\.js", value)
                or ".." in path.parts or path.as_posix() != value):
            raise ValueError("recording spec must be a normalized integrated Playwright file")
        return value

    @field_validator("recorded_at")
    @classmethod
    def valid_recorded_at(cls, value: str) -> str:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("recording time must be ISO format with timezone") from exc
        if "T" not in value or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("recording time must be ISO format with timezone")
        return value

    @model_validator(mode="after")
    def owned_path(self) -> "Recording":
        match = _RECORDING_PATH.fullmatch(self.path)
        if match is None or match.group(2) != self.id:
            raise ValueError("recording path must identify its owned capture and video ID")
        return self


class RecordingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    recordings: list[Recording] = Field(max_length=MAX_RECORDINGS)

    @model_validator(mode="after")
    def unique_ids(self) -> "RecordingManifest":
        validate_unique_recordings(self.recordings)
        return self


def validate_unique_recordings(recordings: Sequence[Recording]) -> None:
    if len({recording.id for recording in recordings}) != len(recordings):
        raise RecordingError("recording IDs must be unique")
    if len({recording.path for recording in recordings}) != len(recordings):
        raise RecordingError("recording paths must be unique")


def _workspace(workspace_path: Path) -> Path:
    try:
        return Path(workspace_path).resolve(strict=True)
    except OSError as exc:
        raise RecordingError("recording workspace is unavailable") from exc


@contextmanager
def _directory(workspace_path: Path, parts: Sequence[str], *, create: bool = False) -> Iterator[int]:
    """Walk every workspace-relative component without following symlinks."""
    descriptor = None
    try:
        descriptor = os.open(_workspace(workspace_path), _DIRECTORY_FLAGS)
        for part in parts:
            if not part or part in {".", ".."} or "/" in part or "\\" in part:
                raise RecordingError("invalid recording directory component")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as exc:
        raise RecordingError("recording directory is unavailable or unsafe") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def create_recording_capture(workspace_path: Path) -> Path:
    """Allocate a new private capture directory; never reuse an existing name."""
    capture_id = uuid.uuid4().hex
    with _directory(workspace_path, (".symphony", "recordings"), create=True) as directory:
        try:
            os.mkdir(capture_id, mode=0o700, dir_fd=directory)
        except OSError as exc:
            raise RecordingError("could not allocate an owned recording capture") from exc
    return _workspace(workspace_path) / ".symphony" / "recordings" / capture_id


@contextmanager
def _open_file(workspace_path: Path, relative_path: str, *, limit: int) -> Iterator[BinaryIO]:
    parts = relative_path.split("/")
    descriptor = None
    try:
        with _directory(workspace_path, parts[:-1]) as directory:
            descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= limit:
            raise RecordingError("recording file is not a bounded, nonempty regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            yield source
    except OSError as exc:
        raise RecordingError("recording file is unavailable or unsafe") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def _check_bytes(source: BinaryIO, recording: Recording, snapshot: BinaryIO | None = None) -> None:
    before = os.fstat(source.fileno())
    if before.st_size != recording.size_bytes:
        raise RecordingError("recording size does not match its saved metadata")
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = source.read(_CHUNK_BYTES)
        if not chunk:
            break
        if total == 0 and not chunk.startswith(WEBM_SIGNATURE):
            raise RecordingError("recording does not have a WebM/EBML signature")
        total += len(chunk)
        if total > recording.size_bytes:
            raise RecordingError("recording grew while being read")
        digest.update(chunk)
        if snapshot is not None:
            snapshot.write(chunk)
    after = os.fstat(source.fileno())
    if (total != recording.size_bytes or _identity(before) != _identity(after)
            or digest.hexdigest() != recording.sha256):
        raise RecordingError("recording changed or does not match its saved SHA-256")


def _recording(value: Recording | Mapping[str, object]) -> Recording:
    try:
        payload = value.model_dump() if isinstance(value, Recording) else value
        return Recording.model_validate(payload)
    except ValueError as exc:
        raise RecordingError("invalid recording metadata") from exc


def open_verified_recording(workspace_path: Path, metadata: Recording | Mapping[str, object]) -> BinaryIO:
    """Return a verified private snapshot; callers must close it after streaming.

    A private spool prevents in-place source changes after verification from
    changing the bytes served to a browser. Large videos spill to an anonymous
    temporary file rather than keeping up to 500 MiB in memory.
    """
    recording = _recording(metadata)
    snapshot = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with _open_file(workspace_path, recording.path, limit=MAX_RECORDING_BYTES) as source:
            _check_bytes(source, recording, snapshot)
        snapshot.seek(0)
        return snapshot
    except (OSError, ValueError) as exc:
        snapshot.close()
        if isinstance(exc, RecordingError):
            raise
        raise RecordingError("recording snapshot could not be verified") from exc
    except BaseException:
        snapshot.close()
        raise


def load_recording_manifest(
    workspace_path: Path, capture_path: Path, selected_specs: Sequence[str],
) -> list[dict]:
    """Load only this invocation's manifest and verify every referenced video."""
    workspace = _workspace(workspace_path)
    try:
        relative_capture = Path(capture_path).relative_to(workspace).as_posix()
    except ValueError as exc:
        raise RecordingError("recording capture is outside its workspace") from exc
    if _CAPTURE_PATH.fullmatch(relative_capture) is None:
        raise RecordingError("invalid recording capture path")
    with _open_file(workspace, relative_capture + "/manifest.json", limit=MAX_MANIFEST_BYTES) as source:
        before = os.fstat(source.fileno())
        content = source.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES or _identity(before) != _identity(os.fstat(source.fileno())):
            raise RecordingError("recording manifest changed while being read")
    try:
        manifest = RecordingManifest.model_validate_json(content)
    except ValueError as exc:
        raise RecordingError("invalid recording manifest") from exc
    for recording in manifest.recordings:
        if Path(recording.path).parent.as_posix() != relative_capture:
            raise RecordingError("manifest references another recording capture")
        if recording.spec not in selected_specs:
            raise RecordingError("manifest references an unselected Playwright spec")
        with _open_file(workspace, recording.path, limit=MAX_RECORDING_BYTES) as source:
            _check_bytes(source, recording)
    return [recording.model_dump(mode="json") for recording in manifest.recordings]
