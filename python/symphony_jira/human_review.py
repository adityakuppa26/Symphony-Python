from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CodexConfig
from .models import Issue, IssueComment, RequirementsSnapshot, RunRecord
from .plan_spec import PlanSpec
from .requirements_artifacts import (
    RequirementsArtifactError,
    canonical_requirements_snapshot_json,
    read_requirements_snapshot_artifact,
)


WORKSPACE_DIFF_TIMEOUT_SECONDS = 15.0
MAX_FROZEN_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_DIFF_BYTES = 16 * 1024 * 1024
MAX_VERIFICATION_EVIDENCE_BYTES = 16 * 1024 * 1024


class HumanReviewContextError(RuntimeError):
    """Frozen completed-review context is missing, unsafe, or has drifted."""


@dataclass(frozen=True)
class WorkspaceDiffSnapshot:
    content: str
    content_hash: str


def capture_workspace_diff(
    workspace_path: Path,
    plan_spec: PlanSpec,
    *,
    managed_repositories: tuple[str | Path, ...] = (),
    include_plan_repositories: bool = True,
    timeout_seconds: float = WORKSPACE_DIFF_TIMEOUT_SECONDS,
) -> WorkspaceDiffSnapshot:
    """Capture a deterministic multi-repository diff and untracked-file digest."""

    workspace_root = workspace_path.resolve()
    digest = hashlib.sha256()
    sections: list[str] = []
    baseline_shas = (
        {
            baseline.repository.strip(): baseline.sha
            for baseline in plan_spec.baseline_repository_shas
        }
        if include_plan_repositories
        else {}
    )
    repository_names = set(baseline_shas)
    repository_names.update(
        Path(repository).as_posix()
        for repository in managed_repositories
    )
    for repository_name in sorted(repository_names):
        repository_path = (workspace_root / repository_name).resolve()
        try:
            repository_path.relative_to(workspace_root)
        except ValueError as exc:
            raise HumanReviewContextError(
                f"review repository {repository_name!r} resolves outside the workspace"
            ) from exc
        if not repository_path.is_dir():
            raise HumanReviewContextError(
                f"review repository {repository_name!r} is missing at {repository_path}"
            )

        actual_head_output = _run_git_bytes(
            repository_path,
            ["rev-parse", "HEAD"],
            timeout_seconds=timeout_seconds,
        )
        _validate_captured_size(
            actual_head_output,
            f"Git HEAD for review repository {repository_name!r}",
        )
        actual_head = actual_head_output.decode("ascii", errors="strict").strip()
        baseline_sha = baseline_shas.get(repository_name)
        if baseline_sha is not None and actual_head != baseline_sha:
            raise HumanReviewContextError(
                f"review repository {repository_name!r} moved from approved HEAD "
                f"{baseline_sha} to {actual_head or 'unknown'}"
            )

        status_output = _run_git_bytes(
            repository_path,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            timeout_seconds=timeout_seconds,
        )
        _validate_captured_size(
            status_output,
            f"Git status for review repository {repository_name!r}",
        )
        diff_output = _run_git_bytes(
            repository_path,
            ["diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            timeout_seconds=timeout_seconds,
        )
        _validate_captured_size(
            diff_output,
            f"Git diff for review repository {repository_name!r}",
        )
        untracked_output = _run_git_bytes(
            repository_path,
            ["ls-files", "--others", "--exclude-standard", "-z"],
            timeout_seconds=timeout_seconds,
        )
        _validate_captured_size(
            untracked_output,
            f"untracked-file list for review repository {repository_name!r}",
        )
        untracked_paths = sorted(
            path
            for path in _nul_paths(untracked_output)
            if not _is_symphony_artifact(path)
        )

        digest.update(b"repository\0")
        digest.update(repository_name.encode("utf-8"))
        digest.update(b"\0head\0")
        digest.update(actual_head.encode("ascii"))
        digest.update(b"\0diff\0")
        digest.update(diff_output)

        status_lines = [
            entry
            for entry in _nul_paths(status_output)
            if not _status_entry_is_symphony_artifact(entry)
        ]
        digest.update(b"\0status\0")
        for entry in status_lines:
            digest.update(entry.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")

        untracked_manifest: list[str] = []
        for relative_name in untracked_paths:
            content_hash, size, kind = _hash_untracked_path(
                repository_path,
                relative_name,
                digest,
            )
            untracked_manifest.append(
                f"{relative_name}\t{kind}\t{size}\tsha256:{content_hash}"
            )

        sections.append(
            "\n".join(
                [
                    f"## Repository {repository_name}",
                    f"HEAD: {actual_head}",
                    "",
                    "### Status",
                    _display_status(status_lines) or "(clean)",
                    "",
                    "### Tracked diff",
                    diff_output.decode("utf-8", errors="replace").rstrip()
                    or "(no tracked diff)",
                    "",
                    "### Untracked files",
                    "\n".join(untracked_manifest) or "(none)",
                ]
            )
        )

    content = "\n\n".join(sections).strip() + "\n"
    _validate_captured_size(
        content.encode("utf-8", errors="surrogateescape"),
        "frozen workspace diff",
    )
    return WorkspaceDiffSnapshot(content=content, content_hash=digest.hexdigest())


def issue_from_frozen_snapshot(
    source_run: RunRecord,
    snapshot: RequirementsSnapshot,
) -> Issue:
    if snapshot.issue_identifier != source_run.issue_identifier:
        raise HumanReviewContextError(
            "frozen requirements snapshot belongs to a different Jira issue"
        )
    if snapshot.issue_id != source_run.issue_id:
        raise HumanReviewContextError(
            "frozen requirements snapshot belongs to a different Jira issue ID"
        )
    expected_hash = str(source_run.issue_fingerprint or "")
    if snapshot.calculate_content_hash() != expected_hash:
        raise HumanReviewContextError(
            "frozen requirements snapshot hash does not match the source run"
        )

    comments = [
        IssueComment(
            id=artifact.source.source_id,
            author=artifact.source.author,
            body=artifact.text,
            created=artifact.source.timestamp,
            updated=artifact.source.timestamp,
            source=artifact.source,
        )
        for artifact in snapshot.comments
    ]
    return Issue(
        id=source_run.issue_id,
        identifier=source_run.issue_identifier,
        title=source_run.issue_identifier,
        description=snapshot.description.text if snapshot.description else None,
        status="Completed - Addressing Human Review",
        url=snapshot.issue_url,
        comments=comments,
        attachments=snapshot.attachments,
        parent=snapshot.parent,
        children=snapshot.children,
        linked_issues=snapshot.linked_issues,
        dependencies=snapshot.dependencies,
        components=snapshot.components,
        versions=snapshot.versions,
        requirements_snapshot=snapshot,
    )


def validate_frozen_snapshot_artifacts(
    workspace_path: Path,
    snapshot_hash: str,
) -> str | None:
    paths = [
        workspace_path / ".symphony" / "requirements-snapshot.json",
        workspace_path
        / ".symphony"
        / "requirements-snapshots"
        / f"{snapshot_hash}.json",
    ]
    for path in paths:
        try:
            snapshot = read_requirements_snapshot_artifact(path)
        except RequirementsArtifactError as exc:
            return f"Frozen requirements artifact is invalid at {path}: {exc}"
        if snapshot.calculate_content_hash() != snapshot_hash:
            return (
                f"Frozen requirements artifact at {path} does not match "
                f"snapshot {snapshot_hash}."
            )
    return None


def read_only_codex_config(config: CodexConfig) -> CodexConfig:
    # Normalize every supported sandbox spelling and remove convenience/bypass
    # flags that could otherwise override a read-only triage pass. Appending one
    # canonical flag also makes duplicate user configuration deterministic.
    source_args = list(config.args)
    args: list[str] = []
    index = 0
    while index < len(source_args):
        argument = source_args[index]
        if argument in {"--sandbox", "-s"}:
            if index + 1 >= len(source_args):
                raise HumanReviewContextError(
                    f"Codex argument {argument!r} is missing its sandbox value"
                )
            index += 2
            continue
        if argument.startswith("--sandbox=") or argument.startswith("-s="):
            index += 1
            continue
        if argument in {
            "--dangerously-bypass-approvals-and-sandbox",
            "--full-auto",
            "--yolo",
        }:
            index += 1
            continue
        if argument == "-c" and index + 1 < len(source_args):
            override = source_args[index + 1].strip().replace(" ", "")
            if override.startswith("sandbox_mode="):
                index += 2
                continue
        args.append(argument)
        index += 1
    args.extend(["--sandbox", "read-only"])
    return config.model_copy(
        update={
            "args": args,
            "output_last_message_file": config.output_human_review_triage_file,
        }
    )


def read_frozen_text_artifact(
    workspace_path: Path,
    artifact_path: str | Path,
    *,
    label: str,
    required: bool = False,
    max_bytes: int = MAX_FROZEN_ARTIFACT_BYTES,
) -> str | None:
    """Read a retained text artifact without following links or special files."""

    content = read_frozen_artifact_bytes(
        workspace_path,
        artifact_path,
        label=label,
        required=required,
        max_bytes=max_bytes,
    )
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HumanReviewContextError(f"{label} is not valid UTF-8") from exc


def write_frozen_text_artifact(
    workspace_path: Path,
    artifact_path: str | Path,
    content: str,
    *,
    label: str,
    max_bytes: int = MAX_FROZEN_ARTIFACT_BYTES,
) -> None:
    """Atomically write a workspace artifact without following links."""

    relative_path = Path(artifact_path)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise HumanReviewContextError(
            f"{label} path must be a safe workspace-relative path: {artifact_path}"
        )
    encoded = content.encode("utf-8")
    if max_bytes <= 0:
        raise ValueError("frozen artifact byte limit must be positive")
    if len(encoded) > max_bytes:
        raise HumanReviewContextError(
            f"{label} exceeds the {max_bytes}-byte retained-artifact limit"
        )

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise HumanReviewContextError(
            "secure retained-artifact writes are unavailable on this platform"
        )

    opened_fds: list[int] = []
    temporary_name: str | None = None
    parent_fd: int | None = None
    try:
        root_fd = os.open(
            os.path.abspath(os.fspath(workspace_path)),
            os.O_RDONLY | directory | nofollow,
        )
        opened_fds.append(root_fd)
        parent_fd = root_fd
        for part in relative_path.parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            parent_fd = os.open(
                part,
                os.O_RDONLY | directory | nofollow,
                dir_fd=parent_fd,
            )
            opened_fds.append(parent_fd)
            metadata = os.fstat(parent_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise HumanReviewContextError(
                    f"{label} parent is not a directory"
                )
            if metadata.st_uid != os.geteuid():
                raise HumanReviewContextError(
                    f"{label} parent is not owned by the current user"
                )

        leaf_name = relative_path.parts[-1]
        temporary_name = f".{leaf_name}.tmp-{secrets.token_hex(12)}"
        artifact_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
            dir_fd=parent_fd,
        )
        opened_fds.append(artifact_fd)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(artifact_fd, remaining)
            if written <= 0:
                raise HumanReviewContextError(f"could not safely write {label}")
            remaining = remaining[written:]
        os.fsync(artifact_fd)
        metadata = os.fstat(artifact_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            raise HumanReviewContextError(
                f"temporary {label} is not a private regular file"
            )
        os.replace(
            temporary_name,
            leaf_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        os.fsync(parent_fd)
    except HumanReviewContextError:
        raise
    except OSError as exc:
        raise HumanReviewContextError(f"could not safely write {label}: {exc}") from exc
    finally:
        if temporary_name is not None and parent_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for descriptor in reversed(opened_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass


def read_frozen_artifact_bytes(
    workspace_path: Path,
    artifact_path: str | Path,
    *,
    label: str,
    required: bool = False,
    max_bytes: int = MAX_FROZEN_ARTIFACT_BYTES,
) -> bytes | None:
    """Read retained artifact bytes without following links or special files."""

    relative_path = Path(artifact_path)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise HumanReviewContextError(
            f"{label} path must be a safe workspace-relative path: {artifact_path}"
        )
    if max_bytes <= 0:
        raise ValueError("frozen artifact byte limit must be positive")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not directory or not nonblock:
        raise HumanReviewContextError(
            "secure retained-artifact reads are unavailable on this platform"
        )

    opened_fds: list[int] = []
    try:
        root_fd = os.open(
            os.path.abspath(os.fspath(workspace_path)),
            os.O_RDONLY | directory | nofollow,
        )
        opened_fds.append(root_fd)
        parent_fd = root_fd
        for part in relative_path.parts[:-1]:
            parent_fd = os.open(
                part,
                os.O_RDONLY | directory | nofollow,
                dir_fd=parent_fd,
            )
            opened_fds.append(parent_fd)
        artifact_fd = os.open(
            relative_path.parts[-1],
            os.O_RDONLY | nofollow | nonblock,
            dir_fd=parent_fd,
        )
        opened_fds.append(artifact_fd)
        metadata = os.fstat(artifact_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise HumanReviewContextError(f"{label} is not a regular file")
        if metadata.st_nlink != 1:
            raise HumanReviewContextError(f"{label} must not be hard-linked")
        if metadata.st_uid != os.geteuid():
            raise HumanReviewContextError(f"{label} is not owned by the current user")
        if metadata.st_size > max_bytes:
            raise HumanReviewContextError(
                f"{label} exceeds the {max_bytes}-byte retained-artifact limit"
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(artifact_fd, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise HumanReviewContextError(
                    f"{label} exceeds the {max_bytes}-byte retained-artifact limit"
                )
        final_metadata = os.fstat(artifact_fd)
        if (
            total != metadata.st_size
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise HumanReviewContextError(f"{label} changed while it was read")
        return b"".join(chunks)
    except FileNotFoundError as exc:
        if required:
            raise HumanReviewContextError(f"{label} is missing") from exc
        return None
    except HumanReviewContextError:
        raise
    except OSError as exc:
        raise HumanReviewContextError(f"could not safely read {label}: {exc}") from exc
    finally:
        for descriptor in reversed(opened_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass


def hash_verification_evidence(
    workspace_path: Path,
    evidence_path: str | Path | None,
) -> str:
    """Hash retained verification evidence and validate referenced runtime logs."""

    workspace_root, relative_path = _workspace_artifact_path(
        workspace_path,
        evidence_path,
        label="verification evidence",
    )
    content = read_frozen_artifact_bytes(
        workspace_root,
        relative_path,
        label="verification evidence",
        required=True,
        max_bytes=MAX_VERIFICATION_EVIDENCE_BYTES,
    )
    if content is None:
        raise HumanReviewContextError("verification evidence is missing")
    try:
        evidence_text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HumanReviewContextError(
            "verification evidence is not valid UTF-8"
        ) from exc
    _validate_runtime_verification_logs(workspace_root, evidence_text)
    return hashlib.sha256(content).hexdigest()


def hash_runtime_verification_log(
    workspace_path: Path,
    log_path: str | Path | None,
) -> str:
    """Hash a runtime verification log's exact retained bytes."""

    return _hash_verification_log(
        workspace_path,
        log_path,
        label="runtime verification log",
    )


def _hash_verification_log(
    workspace_path: Path,
    log_path: str | Path | None,
    *,
    label: str,
) -> str:
    """Hash a retained verification log's exact bytes."""

    workspace_root, relative_path = _workspace_artifact_path(
        workspace_path,
        log_path,
        label=label,
    )
    content = read_frozen_artifact_bytes(
        workspace_root,
        relative_path,
        label=label,
        required=True,
        max_bytes=MAX_VERIFICATION_EVIDENCE_BYTES,
    )
    if content is None:
        raise HumanReviewContextError(f"{label} is missing")
    return hashlib.sha256(content).hexdigest()


def _workspace_artifact_path(
    workspace_path: Path,
    artifact_path: str | Path | None,
    *,
    label: str,
) -> tuple[Path, Path]:
    if artifact_path is None or not str(artifact_path).strip():
        raise HumanReviewContextError(f"{label} path is missing")
    workspace_root = Path(os.path.abspath(os.fspath(workspace_path)))
    candidate = Path(artifact_path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    normalized = Path(os.path.abspath(os.fspath(candidate)))
    try:
        relative_path = normalized.relative_to(workspace_root)
    except ValueError as exc:
        raise HumanReviewContextError(
            f"{label} must be retained inside the workspace"
        ) from exc
    if not relative_path.parts:
        raise HumanReviewContextError(f"{label} path must identify a file")
    return workspace_root, relative_path


def _validate_runtime_verification_logs(
    workspace_path: Path,
    evidence_text: str,
) -> None:
    try:
        manifest = json.loads(evidence_text)
    except json.JSONDecodeError:
        return
    if not _is_runtime_verification_manifest(manifest):
        return

    hook = manifest.get("hook")
    if hook is not None:
        if not isinstance(hook, dict):
            raise HumanReviewContextError(
                "runtime verification manifest hook is invalid"
            )
        output_path = hook.get("output_path")
        expected_output_hash = str(
            hook.get("output_sha256") or ""
        ).strip().lower()
        if output_path is None:
            if expected_output_hash:
                raise HumanReviewContextError(
                    "runtime verification manifest hook has a log hash without "
                    "an output path"
                )
        else:
            if not isinstance(output_path, str) or not output_path.strip():
                raise HumanReviewContextError(
                    "runtime verification manifest hook has an invalid output path"
                )
            if len(expected_output_hash) != 64 or any(
                character not in "0123456789abcdef"
                for character in expected_output_hash
            ):
                raise HumanReviewContextError(
                    "runtime verification manifest hook has no valid output SHA-256"
                )
            actual_output_hash = _hash_verification_log(
                workspace_path,
                output_path,
                label="verification hook log",
            )
            if actual_output_hash != expected_output_hash:
                raise HumanReviewContextError(
                    "verification hook log changed after its manifest was written: "
                    f"{output_path}"
                )

    checks = manifest["runtime"]["checks"]
    if not isinstance(checks, list):
        raise HumanReviewContextError(
            "runtime verification manifest checks are invalid"
        )
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise HumanReviewContextError(
                f"runtime verification manifest check {index} is invalid"
            )
        log_path = check.get("log_path")
        expected_hash = str(check.get("log_sha256") or "").strip().lower()
        if log_path is None:
            if expected_hash:
                raise HumanReviewContextError(
                    f"runtime verification manifest check {index} has a log hash "
                    "without a log path"
                )
            continue
        if not isinstance(log_path, str) or not log_path.strip():
            raise HumanReviewContextError(
                f"runtime verification manifest check {index} has an invalid log path"
            )
        if len(expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            raise HumanReviewContextError(
                f"runtime verification manifest check {index} has no valid log SHA-256"
            )
        actual_hash = hash_runtime_verification_log(workspace_path, log_path)
        if actual_hash != expected_hash:
            raise HumanReviewContextError(
                "runtime verification log changed after its manifest was written: "
                f"{log_path}"
            )


def _is_runtime_verification_manifest(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    runtime = value.get("runtime")
    return (
        value.get("schema_version") == "1.0"
        and isinstance(value.get("issue_identifier"), str)
        and "plan_spec_hash" in value
        and isinstance(value.get("affected_repositories"), list)
        and isinstance(runtime, dict)
        and "checks" in runtime
    )


def build_human_review_triage_prompt(
    *,
    issue: Issue,
    action: dict[str, Any],
    triage_instructions: str,
) -> str:
    return f"""You are triaging pasted human code-review feedback for a completed Symphony run.

This pass is classification only. The sandbox is read-only. Do not edit any file.

Review action ID: {action["id"]}
Source completed run: {action["source_run_id"]}
Reserved result run: {action["result_run_id"]}
Reviewer: {action["reviewer_identity"]}
Review source / PR: {action["source_url"]}

Pasted reviewer comments:
{action["comments"]}

Triage instructions:
{triage_instructions.strip()}

Exact frozen requirements snapshot hash:
{action["requirements_snapshot_hash"]}

Exact frozen requirements snapshot:
{canonical_requirements_snapshot_json(issue.requirements_snapshot).rstrip() if issue.requirements_snapshot else "missing"}

Exact validated PlanSpec hash:
{action.get("plan_spec_hash") or "none"}

Exact validated PlanSpec:
{action.get("plan_spec") or "No PlanSpec was configured."}

Exact frozen AutomationPlan hash:
{action.get("automation_plan_hash") or "none"}

Exact frozen AutomationPlan:
{action.get("automation_plan") or "No automation phase was bound to this run."}

Frozen automation result:
{action.get("automation_result") or "No automation result was bound to this run."}

Exact frozen approval:
{json.dumps(action.get("approval"), ensure_ascii=False, indent=2, sort_keys=True)}

Previous implementation final response:
{action.get("source_final_message") or "none"}

Previous review:
{action.get("source_review") or "none"}

Previous review history:
{action.get("source_review_history") or "none"}

Submission-time workspace diff:
{action.get("workspace_diff") or "(empty)"}

Inspect the live workspace diff as needed and compare it with the frozen context above.
Return exactly one JSON object:
- {{"decision":"code_changes","reason":"..."}} when the comments are code-only and fit the exact PlanSpec and, for automation code, the exact AutomationPlan.
- {{"decision":"automation_plan_changes_required","reason":"..."}} when only the
  derived AutomationPlan must change while the approved development PlanSpec remains exact.
- {{"decision":"plan_changes_required","reason":"..."}} when behavior, scope, architecture,
  acceptance criteria, affected surfaces, compatibility, or non-goals must change.
- {{"decision":"needs_human","question":"..."}} only when the boundary cannot be determined safely.

Product behavior or acceptance criteria absent from the frozen Jira snapshot require
plan_changes_required and authoritative Jira evidence; do not treat pasted review prose
as a new product requirement."""


def build_human_review_implementation_prompt(
    *,
    issue: Issue,
    action: dict[str, Any],
    original_prompt: str,
) -> str:
    return f"""{original_prompt}

Symphony is addressing a human review of completed run {action["source_run_id"]}.
This is not a Jira requirements update. The exact frozen requirements snapshot,
PlanSpec, AutomationPlan when present, and approval remain authoritative.

Human review action: {action["id"]}
Reviewer: {action["reviewer_identity"]}
Review source / PR: {action["source_url"]}

Pasted code-review comments:
{action["comments"]}

Read the retained workspace and current git diff. Apply only code-level feedback that
fits the exact validated PlanSpec. Run relevant verification. Do not change the PlanSpec.

Exact validated PlanSpec hash:
{action.get("plan_spec_hash") or "none"}

Exact validated PlanSpec:
{action.get("plan_spec") or "No PlanSpec was configured."}

Exact frozen AutomationPlan hash:
{action.get("automation_plan_hash") or "none"}

Exact frozen AutomationPlan:
{action.get("automation_plan") or "No automation phase was bound to this run."}

Frozen automation result:
{action.get("automation_result") or "No automation result was bound to this run."}

Do not edit the configured automation checkout in this development pass. If the
feedback affects automation, return automation_plan_changes_required so Symphony
can route it through the isolated automation planning and implementation lane.

If any requested change would alter behavior, scope, architecture, acceptance criteria,
affected surfaces, compatibility, non-goals, or the development PlanSpec, do not edit
for that request. Return:
{{"decision":"plan_changes_required","reason":"<why the approved plan must change>"}}

If only the derived automation plan must change, do not edit for that request. Return:
{{"decision":"automation_plan_changes_required","reason":"<why automation must be replanned>"}}

Otherwise leave a concise final report with files changed, verification, how each pasted
comment was addressed, and residual risk."""


def classify_human_review_triage(message: str) -> tuple[str, str]:
    payload = _parse_json_object(message)
    if payload is None:
        return "invalid", "Triage output was not a JSON object."
    raw_decision = str(
        payload.get("decision") or payload.get("status") or ""
    ).strip().lower()
    normalized = raw_decision.replace("-", "_").replace(" ", "_")
    reason = str(
        payload.get("reason")
        or payload.get("question")
        or payload.get("message")
        or ""
    ).strip()
    if normalized in {
        "code_changes",
        "code_changes_required",
        "changes_required",
        "code_only",
    }:
        return "code_changes", reason
    if normalized in {
        "automation_plan_changes_required",
        "automation_plan_change_required",
        "automation_replan",
        "automation_replanning_required",
    }:
        return "automation_plan_changes_required", reason
    if normalized in {
        "plan_changes_required",
        "plan_change_required",
        "replan",
        "replanning_required",
    }:
        return "plan_changes_required", reason
    if normalized in {
        "needs_human",
        "human_required",
        "requires_human",
    }:
        return "needs_human", reason or "Human review triage needs clarification."
    return "invalid", reason or f"Unrecognized triage decision: {raw_decision or 'missing'}."


def _run_git_bytes(
    repository_path: Path,
    arguments: list[str],
    *,
    timeout_seconds: float,
) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_path), *arguments],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise HumanReviewContextError(
            f"Git {' '.join(arguments)} timed out for {repository_path}"
        ) from exc
    except OSError as exc:
        raise HumanReviewContextError(
            f"Git {' '.join(arguments)} could not run for {repository_path}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        raise HumanReviewContextError(
            f"Git {' '.join(arguments)} failed for {repository_path}: {detail[:500]}"
        )
    return result.stdout


def _validate_captured_size(content: bytes, label: str) -> None:
    if len(content) > MAX_WORKSPACE_DIFF_BYTES:
        raise HumanReviewContextError(
            f"{label} exceeds the {MAX_WORKSPACE_DIFF_BYTES}-byte workspace-diff limit"
        )


def _nul_paths(output: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in output.split(b"\0")
        if item
    ]


def _is_symphony_artifact(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized == ".symphony" or normalized.startswith(".symphony/")


def _status_entry_is_symphony_artifact(entry: str) -> bool:
    path = entry[3:] if len(entry) > 3 else entry
    return entry.startswith("?? ") and _is_symphony_artifact(path)


def _display_status(entries: list[str]) -> str:
    return "\n".join(entry.replace("\n", "\\n") for entry in entries)


def _hash_untracked_path(
    repository_path: Path,
    relative_name: str,
    digest: Any,
) -> tuple[str, int, str]:
    relative_path = Path(relative_name)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise HumanReviewContextError(
            f"unsafe untracked path reported by Git: {relative_name!r}"
        )
    path = repository_path / relative_path
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HumanReviewContextError(
            f"could not inspect untracked path {path}: {exc}"
        ) from exc

    digest.update(b"\0untracked\0")
    digest.update(relative_name.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0mode\0")
    digest.update(str(metadata.st_mode).encode("ascii"))
    digest.update(b"\0size\0")
    digest.update(str(metadata.st_size).encode("ascii"))
    digest.update(b"\0content\0")
    content_digest = hashlib.sha256()

    if stat.S_ISLNK(metadata.st_mode):
        content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        _validate_captured_size(content, f"untracked symlink {relative_name!r}")
        content_digest.update(content)
        digest.update(content)
        return content_digest.hexdigest(), len(content), "symlink"

    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_size > MAX_WORKSPACE_DIFF_BYTES:
            raise HumanReviewContextError(
                f"untracked file {relative_name!r} exceeds the "
                f"{MAX_WORKSPACE_DIFF_BYTES}-byte workspace-diff limit"
            )
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise HumanReviewContextError(
                "secure untracked-file hashing is unavailable on this platform"
            )
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | nofollow)
            opened_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_dev != metadata.st_dev
                or opened_metadata.st_ino != metadata.st_ino
                or opened_metadata.st_mode != metadata.st_mode
                or opened_metadata.st_size != metadata.st_size
            ):
                raise HumanReviewContextError(
                    f"untracked file {relative_name!r} changed while its diff was captured"
                )
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_WORKSPACE_DIFF_BYTES:
                    raise HumanReviewContextError(
                        f"untracked file {relative_name!r} exceeds the "
                        f"{MAX_WORKSPACE_DIFF_BYTES}-byte workspace-diff limit"
                    )
                content_digest.update(chunk)
                digest.update(chunk)
            if total != metadata.st_size:
                raise HumanReviewContextError(
                    f"untracked file {relative_name!r} changed while its diff was captured"
                )
        except HumanReviewContextError:
            raise
        except OSError as exc:
            raise HumanReviewContextError(
                f"could not safely hash untracked file {path}: {exc}"
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return content_digest.hexdigest(), metadata.st_size, "file"

    content = f"mode:{metadata.st_mode}".encode("ascii")
    content_digest.update(content)
    digest.update(content)
    return content_digest.hexdigest(), len(content), "special"


def _parse_json_object(message: str) -> dict[str, Any] | None:
    text = message.strip()
    candidates = [text]
    fence = chr(96) * 3
    if fence in text:
        for part in text.split(fence):
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped:
                candidates.append(stripped)
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None
