from __future__ import annotations

import os
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
    """Controls the material Jira sources included in a requirements snapshot."""

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
    download_attachments: bool = True
    max_attachment_bytes: int = 10 * 1024 * 1024
    attachment_download_max_concurrency: int = 4
    require_attachment_analysis: bool = True
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
    output_last_message_file: str = ".symphony/codex-final.md"
    plan_before_implementation: bool = False
    require_plan_approval: bool = False
    planning_prompt: str = (
        "Before implementation, inspect the relevant code and produce a concise implementation plan/spec. "
        "Include: understood requirement, affected repos/files, proposed change, verification plan, assumptions, and open questions. "
        "Do not make product, UX, data-ordering, default-behavior, or ownership decisions that are not explicitly stated by Jira or clearly established by existing code. "
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

    @field_validator("max_review_iterations")
    @classmethod
    def non_negative_review_iterations(cls, value: int) -> int:
        if value < 0:
            raise ValueError("codex.max_review_iterations must be non-negative")
        return value


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)


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
