from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .environment import DEFAULT_JIRA_ENVIRONMENT_EXCLUDE_PATTERNS


class ConfigError(Exception):
    """Raised when workflow configuration is invalid."""


class JiraAuthConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["pat", "basic"] = "pat"
    token_env: str = "JIRA_TOKEN"
    email_env: str | None = "JIRA_EMAIL"
    token_config_file: Path | None = None
    token_config_key: str | None = None
    email_config_key: str | None = None


class JiraRequirementsConfig(BaseModel):
    """Controls Jira planning evidence and retained issue context."""

    model_config = ConfigDict(extra="ignore")

    custom_fields: list[str] = Field(default_factory=list)
    acceptance_criteria_fields: list[str] = Field(default_factory=list)
    field_authority: dict[str, str] = Field(default_factory=dict)
    description_authority: str = "product"
    comment_authority: str = "product"
    comment_authority_by_author: dict[str, str] = Field(default_factory=dict)
    authority_rank: dict[str, int] = Field(
        default_factory=lambda: {
            "context": 10,
            "supporting_evidence": 10,
            "engineering_context": 20,
            "product": 30,
            "product_owner": 40,
        }
    )
    attachment_authority: str = "supporting_evidence"
    relation_authority: str = "context"
    comment_page_size: int = 100
    related_issue_hydration_max_concurrency: int = 8
    # Compatibility-only controls for the isolated attachment helpers. The v4
    # CLI planning pipeline intentionally does not activate those helpers.
    download_attachments: bool = False
    max_attachment_bytes: int = 10 * 1024 * 1024
    attachment_download_max_concurrency: int = 4
    require_attachment_analysis: bool = False
    attachment_analyzer: Literal["basic", "codex"] = "basic"
    attachment_analysis_timeout_seconds: int = 120
    attachment_pdf_max_pages: int = 4
    attachment_analysis_max_concurrency: int = 1
    attachment_analysis_max_output_characters: int = 12_000
    hydrate_search_results: bool = True
    discover_epic_children: bool = True
    child_issue_jql: str | None = None
    child_issue_max_pages: int = 100
    symphony_comment_patterns: list[str] = Field(
        default_factory=lambda: [
            r"^Codex run started for [A-Z][A-Z0-9_]*-\d+\.",
            r"^Codex run completed for [A-Z][A-Z0-9_]*-\d+\.",
            r"^Codex run failed for [A-Z][A-Z0-9_]*-\d+\.",
            r"^Codex run is blocked for [A-Z][A-Z0-9_]*-\d+\.",
            r"^Codex plan/spec is ready for [A-Z][A-Z0-9_]*-\d+\.",
        ]
    )

    @field_validator(
        "comment_page_size",
        "related_issue_hydration_max_concurrency",
        "max_attachment_bytes",
        "attachment_download_max_concurrency",
        "attachment_analysis_timeout_seconds",
        "attachment_pdf_max_pages",
        "attachment_analysis_max_concurrency",
        "attachment_analysis_max_output_characters",
        "child_issue_max_pages",
    )
    @classmethod
    def positive_requirements_limit(cls, value: int, info) -> int:
        if value <= 0:
            raise ValueError(f"tracker.requirements.{info.field_name} must be positive")
        upper_bounds = {
            "related_issue_hydration_max_concurrency": 32,
            "attachment_download_max_concurrency": 32,
            "attachment_analysis_timeout_seconds": 900,
            "attachment_pdf_max_pages": 20,
            "attachment_analysis_max_concurrency": 4,
            "attachment_analysis_max_output_characters": 50_000,
            "child_issue_max_pages": 1_000,
        }
        maximum = upper_bounds.get(info.field_name)
        if maximum is not None and value > maximum:
            raise ValueError(
                f"tracker.requirements.{info.field_name} must be at most {maximum}"
            )
        return value

    @field_validator("custom_fields", "acceptance_criteria_fields")
    @classmethod
    def unique_non_blank_fields(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("authority_rank", mode="before")
    @classmethod
    def valid_authority_rank(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError(
                "tracker.requirements.authority_rank must be a mapping"
            )
        normalized: dict[str, int] = {
            "context": 10,
            "supporting_evidence": 10,
            "engineering_context": 20,
            "product": 30,
            "product_owner": 40,
        }
        provided: dict[str, int] = {}
        for authority, rank in value.items():
            if not isinstance(authority, str):
                raise ValueError(
                    "tracker.requirements.authority_rank keys must be strings"
                )
            if isinstance(rank, bool) or not isinstance(rank, int):
                raise ValueError(
                    "tracker.requirements.authority_rank values must be integers"
                )
            key = authority.strip().casefold()
            if not key:
                raise ValueError("tracker.requirements.authority_rank keys must not be blank")
            if rank < 0:
                raise ValueError(
                    "tracker.requirements.authority_rank values must be non-negative"
                )
            normalized[key] = rank
            if key in provided and provided[key] != rank:
                raise ValueError(
                    "tracker.requirements.authority_rank contains conflicting "
                    f"normalized key {key!r}"
                )
            provided[key] = rank
        return normalized

    @model_validator(mode="after")
    def configured_authorities_are_ranked(self) -> "JiraRequirementsConfig":
        ranked = self.authority_rank
        if not self.hydrate_search_results:
            raise ValueError(
                "tracker.requirements.hydrate_search_results must be true so "
                "completed-work identity always uses a full Jira snapshot"
            )

        def normalize(value: str, location: str) -> str:
            authority = value.strip().casefold()
            if not authority:
                raise ValueError(
                    f"tracker.requirements.{location} must not be blank"
                )
            if authority not in ranked:
                raise ValueError(
                    f"tracker.requirements.{location} authority {value!r} is not "
                    "present in tracker.requirements.authority_rank"
                )
            return authority

        self.description_authority = normalize(
            self.description_authority, "description_authority"
        )
        self.comment_authority = normalize(
            self.comment_authority, "comment_authority"
        )
        self.attachment_authority = normalize(
            self.attachment_authority, "attachment_authority"
        )
        self.relation_authority = normalize(
            self.relation_authority, "relation_authority"
        )
        self.field_authority = {
            field_id: normalize(authority, f"field_authority.{field_id}")
            for field_id, authority in self.field_authority.items()
        }
        normalized_by_author: dict[str, str] = {}
        for identity, authority in self.comment_authority_by_author.items():
            normalized_identity = identity.strip().casefold()
            if not normalized_identity:
                raise ValueError(
                    "tracker.requirements.comment_authority_by_author keys "
                    "must not be blank"
                )
            normalized_by_author[normalized_identity] = normalize(
                authority,
                f"comment_authority_by_author.{identity}",
            )
        self.comment_authority_by_author = normalized_by_author
        return self


class TrackerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["jira"] = "jira"
    base_url: str = ""
    auth: JiraAuthConfig = Field(default_factory=JiraAuthConfig)
    jql: str = ""
    required_labels: list[str] = Field(default_factory=list)
    active_statuses: list[str] = Field(default_factory=lambda: ["To Do", "In Progress"])
    terminal_statuses: list[str] = Field(
        default_factory=lambda: ["Done", "Closed", "Cancelled", "Canceled", "Duplicate"]
    )
    handoff_status: str | None = None
    comment_on_start: bool = False
    comment_on_finish: bool = True
    requirements: JiraRequirementsConfig = Field(default_factory=JiraRequirementsConfig)

    @field_validator("base_url")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def required_label_set(self) -> set[str]:
        return {label.lower() for label in self.required_labels}


class PollingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    interval_seconds: int = 60

    @field_validator("interval_seconds")
    @classmethod
    def positive_interval(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("polling.interval_seconds must be positive")
        return value


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    root: Path = Field(default_factory=lambda: Path("/tmp/symphony-workspaces"))
    strategy: Literal["git_worktree", "clone", "hook_only"] = "hook_only"
    source_repo: str | None = None
    branch_prefix: str = "codex"


class HooksConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    after_create: str | None = None
    before_run: str | None = None
    verify: str | None = None
    verify_required: bool = False
    after_run: str | None = None
    timeout_seconds: int = 900

    @field_validator("timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("hooks.timeout_seconds must be positive")
        return value


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_concurrent_agents: int = 1
    max_retries: int = 2
    max_retry_backoff_seconds: int = 300
    timeout_seconds: int = 7200

    @field_validator("max_concurrent_agents", "max_retries", "max_retry_backoff_seconds", "timeout_seconds")
    @classmethod
    def non_negative_or_positive(cls, value: int, info) -> int:
        if info.field_name == "max_retries":
            if value < 0:
                raise ValueError("agent.max_retries must be non-negative")
            return value
        if value <= 0:
            raise ValueError(f"agent.{info.field_name} must be positive")
        return value


class CodexConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str = "codex"
    args: list[str] = Field(default_factory=lambda: ["exec", "--json", "--sandbox", "workspace-write"])
    environment_exclude: list[str] = Field(
        default_factory=lambda: list(DEFAULT_JIRA_ENVIRONMENT_EXCLUDE_PATTERNS)
    )
    output_last_message_file: str = ".symphony/codex-final.md"
    plan_before_implementation: bool = False
    require_plan_approval: bool = False
    planning_prompt: str = (
        "Before implementation, inspect the relevant code and produce a concise implementation plan/spec. "
        "Include: understood requirement, affected repos/files, proposed change, verification plan, assumptions, and open questions. "
        "Do not make product, UX, data-ordering, default-behavior, or ownership decisions that are not explicitly stated by Jira or clearly established by existing code. "
        "When Jira requires backward compatibility, existing behavior, or a standard component pattern, inspect and preserve that established repository behavior for incidental edge cases instead of requesting a new product decision. "
        "Ask only when Jira conflicts with the established behavior or no applicable precedent exists and implementation would introduce new user-visible semantics. "
        "Do not edit files during the planning pass. "
        "If multiple reasonable choices exist, ask for clarification instead of choosing silently."
    )
    output_plan_file: str = ".symphony/codex-plan.md"
    review_after_run: bool = False
    review_prompt: str = (
        "Review the code changes for this Jira issue. Return JSON with decision "
        "'approve' or 'changes_required', a findings array, and residual_risk. "
        "Focus on correctness, regressions, tests, and translation consistency."
    )
    max_review_iterations: int = 1
    human_review_triage_prompt: str = (
        "Classify pasted human code-review feedback against the exact frozen "
        "requirements snapshot, validated PlanSpec, approval, prior reviews, and "
        "current workspace diff. Use code_changes only when the comments can be "
        "addressed without changing behavior, scope, architecture, acceptance "
        "criteria, affected surfaces, compatibility, or non-goals. Use "
        "plan_changes_required when any of those must change. Do not edit files "
        "during triage."
    )
    output_human_review_triage_file: str = (
        ".symphony/codex-human-review-triage.md"
    )

    output_review_file: str = ".symphony/codex-review.md"
    output_review_history_file: str = ".symphony/codex-review-history.md"

    @field_validator("command")
    @classmethod
    def command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("codex.command must be non-empty")
        return value

    @field_validator("environment_exclude")
    @classmethod
    def environment_exclude_not_blank(cls, value: list[str]) -> list[str]:
        if any(not pattern.strip() for pattern in value):
            raise ValueError("codex.environment_exclude patterns must be non-empty")
        return value

    @field_validator("max_review_iterations")
    @classmethod
    def non_negative_review_iterations(cls, value: int) -> int:
        if value < 0:
            raise ValueError("codex.max_review_iterations must be non-negative")
        return value


def normalize_automation_relative_path(
    value: str | Path,
    field_name: str,
) -> str:
    """Return a canonical, non-empty path contained by the issue workspace."""

    raw = str(value).strip()
    label = f"automation.{field_name}"
    if not raw or "\x00" in raw:
        raise ValueError(f"{label} must be a non-empty relative path")
    if "\\" in raw:
        raise ValueError(f"{label} must use POSIX path separators")
    path = Path(raw)
    if path.is_absolute():
        raise ValueError(f"{label} must be relative")
    if any(part == ".." for part in path.parts):
        raise ValueError(f"{label} must not contain ..")
    normalized = path.as_posix()
    if normalized == ".":
        raise ValueError(f"{label} must identify a path below the workspace root")
    return normalized


class AutomationConfig(BaseModel):
    """Controls the post-development automation planning and implementation pass."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    workspace_subdir: Path = Path("automation")
    require_plan_approval: bool = False
    planning_prompt: str = (
        "After development, inspect the canonical Jira requirements, the exact "
        "approved development PlanSpec, the development result and code changes, "
        "and the existing automation repository. Produce a focused plan for only "
        "the relevant automated test changes. Do not edit files during this "
        "planning pass. If no automation change is relevant, explicitly plan a "
        "no-op and explain why."
    )
    implementation_prompt: str = (
        "Implement the automation plan in the configured automation repository "
        "only. Keep the changes narrowly tied to the canonical Jira requirements, "
        "approved development PlanSpec, and actual development changes; do not edit "
        "the development repositories or add unrelated refactors. If the plan is "
        "a no-op, make no file changes and report that outcome."
    )
    output_plan_file: str = ".symphony/codex-automation-plan.md"
    output_result_file: str = ".symphony/codex-automation-final.md"
    review_after_run: bool = False
    review_prompt: str = (
        "Review only the automation changes for this Jira issue against the exact "
        "approved AutomationPlan and implemented development behavior. Return JSON "
        "with decision 'approve', 'changes_required', "
        "'automation_plan_changes_required', or 'plan_changes_required', a findings "
        "array, and residual_risk."
    )
    max_review_iterations: int = 1
    output_review_file: str = ".symphony/codex-automation-review.md"
    output_review_history_file: str = (
        ".symphony/codex-automation-review-history.md"
    )

    @field_validator("workspace_subdir")
    @classmethod
    def safe_workspace_subdir(cls, value: Path) -> Path:
        return Path(
            normalize_automation_relative_path(value, "workspace_subdir")
        )

    @field_validator(
        "output_plan_file",
        "output_result_file",
        "output_review_file",
        "output_review_history_file",
    )
    @classmethod
    def safe_output_file(cls, value: str, info) -> str:
        return normalize_automation_relative_path(value, info.field_name)

    @field_validator("planning_prompt", "implementation_prompt", "review_prompt")
    @classmethod
    def prompt_not_blank(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError(f"automation.{info.field_name} must be non-empty")
        return normalized

    @field_validator("max_review_iterations")
    @classmethod
    def non_negative_review_iterations(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                "automation.max_review_iterations must be non-negative"
            )
        return value


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MIN_RUNTIME_OUTPUT_BYTES = 256
MAX_RUNTIME_OUTPUT_BYTES = 16 * 1024 * 1024


def normalize_runtime_workspace_subdir(value: str | Path) -> str:
    """Return the canonical PlanSpec identity for a runtime repository."""

    path = Path(value)
    if path.is_absolute():
        raise ValueError("runtime repository workspace_subdir must be relative")
    if any(part == ".." for part in path.parts):
        raise ValueError("runtime repository workspace_subdir must not contain ..")
    return path.as_posix()


class RuntimeVerificationProfileConfig(BaseModel):
    """An operator-authored command executed inside a Compose service."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    default_args: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 900

    @field_validator("argv", "default_args")
    @classmethod
    def valid_argv(cls, value: list[str], info) -> list[str]:
        if info.field_name == "argv" and not value:
            raise ValueError("runtime.verification_profiles.*.argv must not be empty")
        if any(not arg.strip() or "\x00" in arg for arg in value):
            raise ValueError(
                f"runtime.verification_profiles.*.{info.field_name} entries must "
                "be non-empty strings containing no NUL"
            )
        return value

    @field_validator("environment")
    @classmethod
    def valid_environment(cls, value: dict[str, str]) -> dict[str, str]:
        for name, env_value in value.items():
            if not _ENVIRONMENT_NAME.fullmatch(name):
                raise ValueError(
                    f"runtime verification environment name is invalid: {name!r}"
                )
            if "\x00" in env_value:
                raise ValueError(
                    f"runtime verification environment value for {name!r} contains NUL"
                )
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def positive_runtime_profile_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                "runtime.verification_profiles.*.timeout_seconds must be positive"
            )
        return value

class RuntimeRepositoryConfig(BaseModel):
    """Maps one checkout in a Jira workspace to a Compose service."""

    model_config = ConfigDict(extra="forbid")

    workspace_subdir: Path
    source_env: str
    service: str
    mount_target: str
    dependencies: list[str] = Field(default_factory=list)
    force_recreate_dependencies: list[str] = Field(default_factory=list)
    container_workdir: str | None = None
    verification_profile: str

    @field_validator("workspace_subdir")
    @classmethod
    def safe_workspace_subdir(cls, value: Path) -> Path:
        return Path(normalize_runtime_workspace_subdir(value))

    @field_validator("source_env")
    @classmethod
    def valid_source_env(cls, value: str) -> str:
        if not _ENVIRONMENT_NAME.fullmatch(value):
            raise ValueError(f"runtime repository source_env is invalid: {value!r}")
        return value

    @field_validator("service", "verification_profile")
    @classmethod
    def non_blank_runtime_name(cls, value: str, info) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError(f"runtime repository {info.field_name} must be non-empty")
        return value

    @field_validator("dependencies", "force_recreate_dependencies")
    @classmethod
    def valid_dependencies(cls, value: list[str], info) -> list[str]:
        normalized = list(dict.fromkeys(dependency.strip() for dependency in value))
        if any(not dependency or "\x00" in dependency for dependency in normalized):
            raise ValueError(
                f"runtime repository {info.field_name} must contain non-empty strings"
            )
        return normalized

    @field_validator("mount_target", "container_workdir")
    @classmethod
    def absolute_container_path(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or "\x00" in value:
            raise ValueError(
                f"runtime repository {info.field_name} must be an absolute "
                "container path"
            )
        return value.rstrip("/") or "/"

    @model_validator(mode="after")
    def valid_dependency_relationships(self) -> "RuntimeRepositoryConfig":
        if self.service in self.dependencies:
            raise ValueError(
                "runtime repository service must not list itself as a dependency"
            )
        undeclared = set(self.force_recreate_dependencies).difference(
            self.dependencies
        )
        if undeclared:
            raise ValueError(
                "runtime repository force_recreate_dependencies must be listed "
                "in dependencies: " + ", ".join(sorted(undeclared))
            )
        return self


class RuntimeConfig(BaseModel):
    """Configuration for the serialized, shared Podman Compose runtime lane."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["podman_compose"] = "podman_compose"
    enabled: bool = False
    required: bool = False
    shutdown_after_handoff: bool = False
    command: list[str] = Field(
        default_factory=lambda: ["/usr/bin/podman", "compose"]
    )
    project_directory: Path | None = None
    compose_file: Path | None = None
    env_file: Path | None = None
    project_name: str | None = None
    lock_file: Path | None = None
    lock_timeout_seconds: int = 900
    preview_timeout_seconds: int = 900
    shutdown_grace_seconds: int = 120
    termination_grace_seconds: int = 10
    max_output_bytes: int = 1024 * 1024
    repositories: dict[str, RuntimeRepositoryConfig] = Field(default_factory=dict)
    verification_profiles: dict[str, RuntimeVerificationProfileConfig] = Field(
        default_factory=dict
    )

    @model_validator(mode="before")
    @classmethod
    def default_required_when_enabled(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "required" not in value:
            data = dict(value)
            data["required"] = bool(data.get("enabled", False))
            return data
        return value

    @field_validator("command")
    @classmethod
    def valid_runtime_command(cls, value: list[str]) -> list[str]:
        if not value or any(not arg.strip() or "\x00" in arg for arg in value):
            raise ValueError(
                "runtime.command must contain non-empty arguments containing no NUL"
            )
        return value

    @field_validator("project_name")
    @classmethod
    def valid_project_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("runtime.project_name must be non-empty")
        return value

    @field_validator(
        "lock_timeout_seconds",
        "preview_timeout_seconds",
        "shutdown_grace_seconds",
        "termination_grace_seconds",
        "max_output_bytes",
    )
    @classmethod
    def positive_runtime_limit(cls, value: int, info) -> int:
        if value <= 0:
            raise ValueError(f"runtime.{info.field_name} must be positive")
        if (
            info.field_name == "max_output_bytes"
            and value < MIN_RUNTIME_OUTPUT_BYTES
        ):
            raise ValueError(
                "runtime.max_output_bytes must be at least "
                f"{MIN_RUNTIME_OUTPUT_BYTES}"
            )
        if (
            info.field_name == "max_output_bytes"
            and value > MAX_RUNTIME_OUTPUT_BYTES
        ):
            raise ValueError(
                "runtime.max_output_bytes must be at most "
                f"{MAX_RUNTIME_OUTPUT_BYTES} so retained logs remain verifiable"
            )
        return value

    @model_validator(mode="after")
    def valid_enabled_runtime(self) -> "RuntimeConfig":
        self.repository_keys_by_workspace_subdir()
        if not self.enabled:
            if self.required:
                raise ValueError("runtime.required cannot be true when runtime is disabled")
            return self
        missing = [
            name
            for name in (
                "project_directory",
                "compose_file",
                "env_file",
                "project_name",
                "lock_file",
            )
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                "enabled runtime is missing required fields: " + ", ".join(missing)
            )
        if not self.repositories:
            raise ValueError("enabled runtime must configure at least one repository")
        if not self.verification_profiles:
            raise ValueError(
                "enabled runtime must configure at least one verification profile"
            )
        unknown_profiles = {
            repository.verification_profile
            for repository in self.repositories.values()
            if repository.verification_profile not in self.verification_profiles
        }
        if unknown_profiles:
            raise ValueError(
                "runtime repositories reference unknown verification profiles: "
                + ", ".join(sorted(unknown_profiles))
            )
        source_envs = [
            repository.source_env for repository in self.repositories.values()
        ]
        if len(source_envs) != len(set(source_envs)):
            raise ValueError("runtime repository source_env values must be unique")
        return self

    def repository_keys_by_workspace_subdir(self) -> dict[str, str]:
        """Map canonical workspace-relative PlanSpec paths to runtime keys."""

        keys: dict[str, str] = {}
        for key, repository in self.repositories.items():
            workspace_subdir = normalize_runtime_workspace_subdir(
                repository.workspace_subdir
            )
            previous_key = keys.get(workspace_subdir)
            if previous_key is not None and previous_key != key:
                raise ValueError(
                    "runtime repositories must use unique normalized "
                    "workspace_subdir values: "
                    f"{workspace_subdir!r} is configured by {previous_key!r} "
                    f"and {key!r}"
                )
            keys[workspace_subdir] = key
        return keys

    def repository_key_for_workspace_subdir(self, value: str | Path) -> str:
        """Resolve a PlanSpec repository path to its runtime mapping key."""

        workspace_subdir = normalize_runtime_workspace_subdir(value)
        try:
            return self.repository_keys_by_workspace_subdir()[workspace_subdir]
        except KeyError as exc:
            raise ValueError(
                "runtime repository workspace_subdir is not configured: "
                f"{workspace_subdir}"
            ) from exc


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def valid_automation_layout(self) -> "WorkflowConfig":
        """Keep enabled automation artifacts and repositories in separate lanes."""

        if not self.automation.enabled:
            return self

        repository = self.automation.workspace_subdir
        if repository.parts and repository.parts[0] == ".symphony":
            raise ValueError(
                "automation.workspace_subdir must not use the reserved .symphony path"
            )

        plan_path = Path(self.automation.output_plan_file)
        result_path = Path(self.automation.output_result_file)
        review_path = Path(self.automation.output_review_file)
        review_history_path = Path(
            self.automation.output_review_history_file
        )
        automation_artifacts = (
            ("output_plan_file", plan_path),
            ("output_result_file", result_path),
            ("output_review_file", review_path),
            ("output_review_history_file", review_history_path),
        )
        def paths_overlap(left: Path, right: Path) -> bool:
            return left == right or left in right.parents or right in left.parents

        for index, (field_name, artifact_path) in enumerate(
            automation_artifacts
        ):
            for other_name, other_path in automation_artifacts[index + 1 :]:
                if paths_overlap(artifact_path, other_path):
                    raise ValueError(
                        f"automation.{field_name} and automation.{other_name} "
                        "must not overlap"
                    )
            if artifact_path == repository or repository in artifact_path.parents:
                raise ValueError(
                    f"automation.{field_name} must be outside the automation checkout"
                )
            if not artifact_path.parts or artifact_path.parts[0] != ".symphony":
                raise ValueError(
                    f"automation.{field_name} must be stored under .symphony"
                )

        codex_artifacts = {
            Path(os.path.normpath(str(path).strip())): field_name
            for field_name, path in (
                ("output_last_message_file", self.codex.output_last_message_file),
                ("output_plan_file", self.codex.output_plan_file),
                ("output_review_file", self.codex.output_review_file),
                (
                    "output_review_history_file",
                    self.codex.output_review_history_file,
                ),
                (
                    "output_human_review_triage_file",
                    self.codex.output_human_review_triage_file,
                ),
            )
        }
        for field_name, artifact_path in automation_artifacts:
            for codex_path, codex_field in codex_artifacts.items():
                if paths_overlap(artifact_path, codex_path):
                    raise ValueError(
                        f"automation.{field_name} must not overlap "
                        f"codex.{codex_field}"
                    )
        for codex_path, codex_field in codex_artifacts.items():
            if paths_overlap(codex_path, repository):
                raise ValueError(
                    f"codex.{codex_field} must not overlap the automation checkout"
                )

        for runtime_name, runtime_repository in self.runtime.repositories.items():
            runtime_path = runtime_repository.workspace_subdir
            if (
                repository == runtime_path
                or repository in runtime_path.parents
                or runtime_path in repository.parents
            ):
                raise ValueError(
                    "automation.workspace_subdir must not overlap runtime repository "
                    f"{runtime_name!r} at {runtime_path.as_posix()!r}"
                )
        return self


class ValidationIssue(BaseModel):
    code: str
    message: str


def load_config(
    raw_config: Mapping[str, object] | None,
    workflow_path: Path,
    environ: Mapping[str, str] | None = None,
) -> WorkflowConfig:
    environ = environ or os.environ
    try:
        config = WorkflowConfig.model_validate(raw_config or {})
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc

    workflow_dir = workflow_path.resolve().parent
    config.workspace.root = resolve_path(config.workspace.root, workflow_dir, environ)
    if config.workspace.source_repo and not looks_like_url(config.workspace.source_repo):
        config.workspace.source_repo = str(resolve_path(config.workspace.source_repo, workflow_dir, environ))
    if config.tracker.auth.token_config_file:
        config.tracker.auth.token_config_file = resolve_path(config.tracker.auth.token_config_file, workflow_dir, environ)
    for field_name in ("project_directory", "compose_file", "env_file", "lock_file"):
        value = getattr(config.runtime, field_name)
        if value is not None:
            setattr(
                config.runtime,
                field_name,
                resolve_path(value, workflow_dir, environ),
            )
    if config.runtime.command and "/" in config.runtime.command[0]:
        config.runtime.command[0] = str(
            resolve_path(config.runtime.command[0], workflow_dir, environ)
        )
    return config


def resolve_path(value: str | Path, base_dir: Path, environ: Mapping[str, str]) -> Path:
    raw = os.path.expanduser(os.path.expandvars(str(value)))
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def looks_like_url(value: str) -> bool:
    return "://" in value or value.startswith("git@")


def validate_preflight(
    workflow_path: Path,
    config: WorkflowConfig,
    environ: Mapping[str, str] | None = None,
    check_jira_credentials: bool = True,
    check_codex: bool = True,
) -> list[ValidationIssue]:
    environ = environ or os.environ
    issues: list[ValidationIssue] = []

    if not workflow_path.exists():
        issues.append(ValidationIssue(code="workflow_missing", message=f"Workflow file does not exist: {workflow_path}"))

    if config.tracker.kind != "jira":
        issues.append(ValidationIssue(code="tracker_kind", message="tracker.kind must be jira"))
    if not config.tracker.base_url:
        issues.append(ValidationIssue(code="jira_base_url", message="tracker.base_url is required"))
    if not config.tracker.jql.strip():
        issues.append(ValidationIssue(code="jira_jql", message="tracker.jql is required"))

    token_env = config.tracker.auth.token_env
    token_value = resolve_configured_secret(
        env_name=token_env,
        config_file=config.tracker.auth.token_config_file,
        config_key=config.tracker.auth.token_config_key,
        environ=environ,
    )
    if check_jira_credentials and not token_value:
        issues.append(
            ValidationIssue(
                code="jira_token_missing",
                message=jira_secret_missing_message("Jira token", token_env, config.tracker.auth.token_config_file, config.tracker.auth.token_config_key),
            )
        )
    email_value = resolve_configured_secret(
        env_name=config.tracker.auth.email_env,
        config_file=config.tracker.auth.token_config_file,
        config_key=config.tracker.auth.email_config_key,
        environ=environ,
    )
    if (
        check_jira_credentials
        and config.tracker.auth.mode == "basic"
        and config.tracker.auth.email_env
        and not email_value
    ):
        issues.append(
            ValidationIssue(
                code="jira_email_missing",
                message=jira_secret_missing_message(
                    "Jira email",
                    config.tracker.auth.email_env,
                    config.tracker.auth.token_config_file,
                    config.tracker.auth.email_config_key,
                ),
            )
        )

    try:
        config.workspace.root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        issues.append(ValidationIssue(code="workspace_root", message=f"Cannot create workspace.root: {exc}"))

    if config.workspace.strategy == "git_worktree":
        if not config.workspace.source_repo:
            issues.append(ValidationIssue(code="source_repo_missing", message="workspace.source_repo is required"))
        elif not Path(config.workspace.source_repo).exists():
            issues.append(
                ValidationIssue(
                    code="source_repo_missing",
                    message=f"workspace.source_repo does not exist: {config.workspace.source_repo}",
                )
            )

    if config.runtime.enabled:
        runtime_command = config.runtime.command
        if find_executable(runtime_command[0]) is None:
            issues.append(
                ValidationIssue(
                    code="runtime_command_missing",
                    message=(
                        "Runtime command was not found or is not executable: "
                        f"{runtime_command[0]}"
                    ),
                )
            )
        if len(runtime_command) < 2:
            issues.append(
                ValidationIssue(
                    code="runtime_provider_missing",
                    message=(
                        "runtime.command must include the Podman Compose provider "
                        "subcommand"
                    ),
                )
            )
        project_directory = config.runtime.project_directory
        if project_directory is None or not project_directory.is_dir():
            issues.append(
                ValidationIssue(
                    code="runtime_project_directory",
                    message=(
                        "Runtime project directory does not exist or is not a "
                        f"directory: {project_directory}"
                    ),
                )
            )
        for code, label, path in (
            (
                "runtime_compose_file",
                "Compose file",
                config.runtime.compose_file,
            ),
            ("runtime_env_file", "environment file", config.runtime.env_file),
        ):
            if path is None or not path.is_file():
                issues.append(
                    ValidationIssue(
                        code=code,
                        message=f"Runtime {label} does not exist or is not a file: {path}",
                    )
                )

    if check_codex:
        command_path = find_executable(config.codex.command)
        if command_path is None:
            issues.append(ValidationIssue(code="codex_missing", message=f"Codex command not found: {config.codex.command}"))
        else:
            try:
                subprocess.run(
                    [str(command_path), "--version"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                issues.append(ValidationIssue(code="codex_version_failed", message=f"Codex version check failed: {exc}"))

    return issues


def find_executable(command: str) -> Path | None:
    if "/" in command:
        path = Path(command).expanduser()
        return path if path.exists() and os.access(path, os.X_OK) else None
    found = shutil.which(command)
    return Path(found) if found else None


def resolve_configured_secret(
    *,
    env_name: str | None,
    config_file: Path | None,
    config_key: str | None,
    environ: Mapping[str, str],
) -> str | None:
    if env_name and environ.get(env_name):
        return environ[env_name]
    if config_file and config_key:
        return read_simple_toml_value(config_file, config_key)
    return None


def read_simple_toml_value(path: Path, key: str) -> str | None:
    try:
        text = path.expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    wanted = key.strip()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() != wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value
    return None


def jira_secret_missing_message(
    label: str,
    env_name: str | None,
    config_file: Path | None,
    config_key: str | None,
) -> str:
    parts = []
    if env_name:
        parts.append(f"environment variable {env_name}")
    if config_file and config_key:
        parts.append(f"{config_key} in {config_file}")
    source = " or ".join(parts) if parts else "configured secret source"
    return f"{label} is not set via {source}"
