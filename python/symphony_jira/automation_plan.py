from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .plan_spec import PlanSpec


AUTOMATION_PLAN_VERSION = "1.0"


class AutomationPlanError(ValueError):
    """Raised when an automation-planning response is invalid or stale."""


class StrictAutomationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class AutomationScenario(StrictAutomationModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    requirement_ids: list[str]
    acceptance_criterion_ids: list[str]

    @model_validator(mode="after")
    def require_traceability(self) -> AutomationScenario:
        if not self.requirement_ids and not self.acceptance_criterion_ids:
            raise ValueError(
                "an automation scenario must reference a development requirement "
                "or acceptance criterion"
            )
        _require_unique(self.requirement_ids, "scenario requirement ID")
        _require_unique(
            self.acceptance_criterion_ids,
            "scenario acceptance-criterion ID",
        )
        return self


class AutomationFileChange(StrictAutomationModel):
    path: str = Field(min_length=1)
    change_type: Literal["add", "update", "delete"]
    description: str = Field(min_length=1)
    scenario_ids: list[str] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = _safe_relative_path(value, label="affected file path")
        if PurePosixPath(normalized).parts[0] in {".git", ".symphony"}:
            raise ValueError(
                "affected file path must not target repository metadata or "
                "Symphony artifacts"
            )
        return normalized

    @field_validator("scenario_ids")
    @classmethod
    def unique_scenario_ids(cls, value: list[str]) -> list[str]:
        _require_unique(value, "file-change scenario ID")
        return value


class AutomationVerification(StrictAutomationModel):
    id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    scenario_ids: list[str] = Field(min_length=1)

    @field_validator("scenario_ids")
    @classmethod
    def unique_scenario_ids(cls, value: list[str]) -> list[str]:
        _require_unique(value, "verification scenario ID")
        return value


class AutomationRisk(StrictAutomationModel):
    id: str = Field(min_length=1)
    severity: Literal["low", "medium", "high", "critical"]
    description: str = Field(min_length=1)
    mitigation: str = Field(min_length=1)


class AutomationAssumption(StrictAutomationModel):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    needs_human: bool


class AutomationOpenQuestion(StrictAutomationModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    blocks_implementation: bool


class AutomationPlan(StrictAutomationModel):
    schema_version: Literal["1.0"]
    decision: Literal["update_required", "no_update_required"]
    issue_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
    requirements_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    development_plan_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    development_workspace_diff_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    automation_repository: str = Field(min_length=1)
    repository_baseline_sha: str = Field(
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    rationale: str = Field(min_length=1)
    mapped_scenarios: list[AutomationScenario]
    affected_file_changes: list[AutomationFileChange]
    verification: list[AutomationVerification]
    risks: list[AutomationRisk]
    assumptions: list[AutomationAssumption]
    open_questions: list[AutomationOpenQuestion]

    @field_validator("automation_repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        return _safe_relative_path(
            value,
            label="automation repository",
            allow_repository_root=True,
            require_canonical=True,
        )

    @model_validator(mode="after")
    def validate_internal_consistency(self) -> AutomationPlan:
        if self.decision == "update_required":
            if not self.mapped_scenarios:
                raise ValueError(
                    "update_required must include at least one mapped scenario"
                )
            if not self.affected_file_changes:
                raise ValueError(
                    "update_required must include at least one affected file change"
                )
            if not self.verification:
                raise ValueError(
                    "update_required must include at least one verification"
                )
        else:
            populated = [
                name
                for name, values in (
                    ("mapped_scenarios", self.mapped_scenarios),
                    ("affected_file_changes", self.affected_file_changes),
                    ("verification", self.verification),
                )
                if values
            ]
            if populated:
                raise ValueError(
                    "no_update_required must keep mapped_scenarios, "
                    "affected_file_changes, and verification empty"
                )

        scenario_ids = [scenario.id for scenario in self.mapped_scenarios]
        _require_unique(scenario_ids, "automation scenario ID")
        valid_scenario_ids = set(scenario_ids)

        paths = [change.path for change in self.affected_file_changes]
        _require_unique(paths, "affected file path")
        for change in self.affected_file_changes:
            _require_references(
                change.scenario_ids,
                valid_scenario_ids,
                f"affected file {change.path!r} scenario",
            )

        _require_unique(
            [verification.id for verification in self.verification],
            "verification ID",
        )
        verified_scenarios: list[str] = []
        for verification in self.verification:
            _require_references(
                verification.scenario_ids,
                valid_scenario_ids,
                f"verification {verification.id!r} scenario",
            )
            verified_scenarios.extend(verification.scenario_ids)
        _require_exact_coverage(
            verified_scenarios,
            scenario_ids,
            "verification must cover every mapped scenario",
        )

        _require_unique([risk.id for risk in self.risks], "risk ID")
        _require_unique(
            [assumption.id for assumption in self.assumptions],
            "assumption ID",
        )
        _require_unique(
            [question.id for question in self.open_questions],
            "open-question ID",
        )

        return self

    def canonical_json(self, *, indent: int | None = None) -> str:
        if indent is not None:
            return json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=indent,
                sort_keys=True,
            )
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def blocking_question(self) -> str | None:
        for question in self.open_questions:
            if question.blocks_implementation:
                return question.question
        for assumption in self.assumptions:
            if assumption.needs_human:
                return assumption.statement
        return None


def automation_result_content_hash(content: str) -> str:
    """Hash the normalized, non-empty automation completion report."""

    normalized = content.strip()
    if not normalized:
        raise AutomationPlanError("Automation completion result is empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_automation_plan(
    message: str,
    *,
    expected_issue_key: str,
    expected_requirements_snapshot_hash: str,
    expected_development_plan_spec_hash: str,
    expected_development_diff_hash: str,
    expected_repository: str,
    expected_repository_baseline_sha: str,
    development_plan_spec: PlanSpec,
) -> AutomationPlan:
    """Parse and bind one JSON automation plan to its exact development inputs."""

    if not message or not message.strip():
        raise AutomationPlanError("automation planning output is empty")

    validation_errors: list[str] = []
    saw_object = False
    for candidate in _json_candidates(message):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        saw_object = True
        if str(payload.get("decision") or "").strip().lower() == "needs_human":
            question = str(
                payload.get("question") or payload.get("reason") or ""
            ).strip()
            suffix = f": {question}" if question else ""
            raise AutomationPlanError(
                f"automation planning response requests human clarification{suffix}"
            )
        try:
            plan = AutomationPlan.model_validate(payload)
        except ValidationError as exc:
            validation_errors.append(_format_validation_error(exc))
            continue
        _validate_context(
            plan,
            expected_issue_key=expected_issue_key,
            expected_requirements_snapshot_hash=expected_requirements_snapshot_hash,
            expected_development_plan_spec_hash=expected_development_plan_spec_hash,
            expected_development_diff_hash=expected_development_diff_hash,
            expected_repository=expected_repository,
            expected_repository_baseline_sha=expected_repository_baseline_sha,
            development_plan_spec=development_plan_spec,
        )
        return plan

    if validation_errors:
        raise AutomationPlanError(f"invalid AutomationPlan: {validation_errors[0]}")
    if saw_object:
        raise AutomationPlanError(
            "automation planning output contains JSON, but not a valid "
            "AutomationPlan object"
        )
    raise AutomationPlanError(
        "automation planning output must contain one complete JSON "
        "AutomationPlan object"
    )


def automation_plan_json_schema() -> str:
    return json.dumps(
        AutomationPlan.model_json_schema(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _validate_context(
    plan: AutomationPlan,
    *,
    expected_issue_key: str,
    expected_requirements_snapshot_hash: str,
    expected_development_plan_spec_hash: str,
    expected_development_diff_hash: str,
    expected_repository: str,
    expected_repository_baseline_sha: str,
    development_plan_spec: PlanSpec,
) -> None:
    bindings = (
        ("issue_key", plan.issue_key, expected_issue_key),
        (
            "requirements_snapshot_hash",
            plan.requirements_snapshot_hash,
            expected_requirements_snapshot_hash,
        ),
        (
            "development_plan_spec_hash",
            plan.development_plan_spec_hash,
            expected_development_plan_spec_hash,
        ),
        (
            "development_workspace_diff_hash",
            plan.development_workspace_diff_hash,
            expected_development_diff_hash,
        ),
        ("automation_repository", plan.automation_repository, expected_repository),
        (
            "repository_baseline_sha",
            plan.repository_baseline_sha,
            expected_repository_baseline_sha,
        ),
    )
    for name, actual, expected in bindings:
        if actual != expected:
            raise AutomationPlanError(
                f"AutomationPlan {name} {actual!r} does not match the exact "
                f"caller binding {expected!r}"
            )

    if development_plan_spec.content_hash() != expected_development_plan_spec_hash:
        raise AutomationPlanError(
            "expected_development_plan_spec_hash does not match the supplied "
            "development PlanSpec"
        )
    if development_plan_spec.issue_key != expected_issue_key:
        raise AutomationPlanError(
            "supplied development PlanSpec issue_key does not match the caller binding"
        )
    if (
        development_plan_spec.requirements_snapshot_hash
        != expected_requirements_snapshot_hash
    ):
        raise AutomationPlanError(
            "supplied development PlanSpec requirements_snapshot_hash does not "
            "match the caller binding"
        )

    if plan.decision == "no_update_required":
        return

    valid_requirement_ids = {
        requirement.id for requirement in development_plan_spec.requirements
    }
    valid_acceptance_ids = {
        criterion.id
        for requirement in development_plan_spec.requirements
        for criterion in requirement.acceptance_criteria
    }
    for scenario in plan.mapped_scenarios:
        _require_references(
            scenario.requirement_ids,
            valid_requirement_ids,
            f"scenario {scenario.id!r} development requirement",
            error_type=AutomationPlanError,
        )
        _require_references(
            scenario.acceptance_criterion_ids,
            valid_acceptance_ids,
            f"scenario {scenario.id!r} development acceptance criterion",
            error_type=AutomationPlanError,
        )


def _safe_relative_path(
    value: str,
    *,
    label: str,
    allow_repository_root: bool = False,
    require_canonical: bool = False,
) -> str:
    normalized = value.replace("\\", "/")
    if "\x00" in normalized or re.match(r"^[A-Za-z]:($|/)", normalized):
        raise ValueError(f"{label} {value!r} must be a safe relative POSIX path")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} {value!r} must be a safe relative POSIX path")
    canonical = path.as_posix()
    if canonical == "." and not allow_repository_root:
        raise ValueError(f"{label} must identify a file within the repository")
    if require_canonical and value != canonical:
        raise ValueError(
            f"{label} {value!r} must use its canonical relative POSIX identity "
            f"{canonical!r}"
        )
    return canonical


def _require_unique(values: list[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}(s): {', '.join(duplicates)}")


def _require_references(
    values: list[str],
    valid_values: set[str],
    label: str,
    *,
    error_type: type[ValueError] = ValueError,
) -> None:
    unknown = sorted(set(values) - valid_values)
    if unknown:
        raise error_type(f"unknown {label}(s): {', '.join(unknown)}")


def _require_exact_coverage(
    actual: list[str],
    expected: list[str] | set[str],
    message: str,
    *,
    error_type: type[ValueError] = ValueError,
) -> None:
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise error_type(f"{message}; missing: {', '.join(missing)}")


def _json_candidates(message: str) -> list[str]:
    text = message.strip()
    candidates = [text]
    if "```" in text:
        for part in text.split("```"):
            stripped = part.strip()
            if stripped.lower().startswith("json"):
                stripped = stripped[4:].lstrip()
            if stripped:
                candidates.append(stripped)

    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        candidates.append(text[index : index + end])

    seen: set[str] = set()
    return [
        candidate
        for candidate in candidates
        if not (candidate in seen or seen.add(candidate))
    ]


def _format_validation_error(error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"])
        details.append(f"{location or 'root'}: {item['msg']}")
    return "; ".join(details)
