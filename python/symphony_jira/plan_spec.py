from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import RequirementsSnapshot


PLAN_SPEC_VERSION = "1.0"

_ACTIVE_JIRA_SOURCE_TYPES = frozenset({"description", "custom_field", "comment"})
_LEGACY_REQUIREMENTS_SCHEMA_VERSIONS = frozenset(
    {
        "jira-requirements/v1",
        "jira-requirements/v2",
        "jira-requirements/v3",
    }
)


class PlanSpecError(ValueError):
    """Raised when a planning response is not a valid, context-bound PlanSpec."""


class StrictPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class JiraSource(StrictPlanModel):
    issue_key: str = Field(min_length=1)
    # Context-only source types remain parseable solely so an exact,
    # already-approved pre-v4 PlanSpec can be integrity-checked during a
    # verification-only migration resume. Current context validation never
    # authorizes them.
    source_type: Literal[
        "description",
        "custom_field",
        "comment",
        "attachment",
        "parent",
        "child",
        "linked_issue",
        "dependency",
        "component",
        "version",
    ]
    source_id: str = Field(min_length=1)
    location: str | None = None
    url: str | None = None


class AcceptanceCriterion(StrictPlanModel):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    jira_sources: list[JiraSource] = Field(min_length=1)


class PlanRequirement(StrictPlanModel):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    jira_sources: list[JiraSource] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)


class RoleStateMatrixEntry(StrictPlanModel):
    canonical_role: Literal["gc", "sub", "gc_as_sub", "all", "other"]
    role: str = Field(min_length=1)
    state: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    requirement_ids: list[str]
    acceptance_criterion_ids: list[str]

    @model_validator(mode="after")
    def require_traceability(self) -> RoleStateMatrixEntry:
        if not self.requirement_ids and not self.acceptance_criterion_ids:
            raise ValueError(
                "a role/state entry must reference a requirement or acceptance criterion"
            )
        return self


class RepositoryBaseline(StrictPlanModel):
    repository: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")


class SurfaceImpact(StrictPlanModel):
    repository: str = Field(min_length=1)
    target: str = Field(min_length=1)
    change: str = Field(min_length=1)


class AffectedSurface(StrictPlanModel):
    repositories: list[str] = Field(min_length=1)
    files: list[SurfaceImpact]
    apis: list[SurfaceImpact]
    schemas: list[SurfaceImpact]
    migrations: list[SurfaceImpact]
    translations: list[SurfaceImpact]


class ExistingPrecedent(StrictPlanModel):
    repository: str = Field(min_length=1)
    path: str = Field(min_length=1)
    description: str = Field(min_length=1)


class PlanAssumption(StrictPlanModel):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    risk: Literal["low", "medium", "high"]
    needs_human: bool


class PlanTestCase(StrictPlanModel):
    id: str = Field(min_length=1)
    acceptance_criterion_id: str = Field(min_length=1)
    level: Literal["unit", "integration", "contract", "end_to_end", "manual"]
    description: str = Field(min_length=1)
    expected_result: str = Field(min_length=1)


class PlanRisk(StrictPlanModel):
    id: str = Field(min_length=1)
    severity: Literal["low", "medium", "high", "critical"]
    description: str = Field(min_length=1)
    mitigation: str = Field(min_length=1)


class OpenQuestion(StrictPlanModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    blocks_implementation: bool
    jira_sources: list[JiraSource]


class BoundedChildPlan(StrictPlanModel):
    id: str = Field(min_length=1)
    issue_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
    scope: str = Field(min_length=1)
    non_goals: list[str]
    requirement_ids: list[str] = Field(min_length=1)
    acceptance_criterion_ids: list[str] = Field(min_length=1)


class EpicStrategy(StrictPlanModel):
    mode: Literal["decomposed", "single_change"]
    rationale: str = Field(min_length=1)
    bounded_child_plans: list[BoundedChildPlan]
    requires_explicit_single_change_approval: bool

    @model_validator(mode="after")
    def validate_mode(self) -> EpicStrategy:
        if self.mode == "decomposed":
            if not self.bounded_child_plans:
                raise ValueError("a decomposed Epic must contain at least one bounded child plan")
            if self.requires_explicit_single_change_approval:
                raise ValueError("a decomposed Epic cannot require single-change approval")
        else:
            if self.bounded_child_plans:
                raise ValueError("a single-change Epic cannot also contain child plans")
            if not self.requires_explicit_single_change_approval:
                raise ValueError("a single-change Epic must require explicit approval")
        return self


class PlanSpec(StrictPlanModel):
    schema_version: Literal["1.0"]
    decision: Literal["ready_for_approval"]
    issue_key: str = Field(min_length=1)
    requirements_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_repository_shas: list[RepositoryBaseline] = Field(min_length=1)
    requirements: list[PlanRequirement] = Field(min_length=1)
    role_state_matrix: list[RoleStateMatrixEntry]
    affected_surface: AffectedSurface
    existing_precedents: list[ExistingPrecedent]
    simplest_implementation: str = Field(min_length=1)
    assumptions: list[PlanAssumption]
    non_goals: list[str]
    prohibited_scope: list[str]
    test_cases: list[PlanTestCase] = Field(min_length=1)
    rollout: str = Field(min_length=1)
    rollback: str = Field(min_length=1)
    compatibility: str = Field(min_length=1)
    risks: list[PlanRisk]
    open_questions: list[OpenQuestion]
    epic_strategy: EpicStrategy | None

    @model_validator(mode="after")
    def validate_traceability(self) -> PlanSpec:
        requirement_ids = [requirement.id for requirement in self.requirements]
        _require_unique(requirement_ids, "requirement ID")

        acceptance_ids = [
            criterion.id
            for requirement in self.requirements
            for criterion in requirement.acceptance_criteria
        ]
        _require_unique(acceptance_ids, "acceptance-criterion ID")

        test_ids = [test.id for test in self.test_cases]
        _require_unique(test_ids, "test-case ID")
        mapped_acceptance_ids = [test.acceptance_criterion_id for test in self.test_cases]
        _require_exact_members(
            mapped_acceptance_ids,
            acceptance_ids,
            "test cases must cover every acceptance criterion",
        )

        valid_requirement_ids = set(requirement_ids)
        valid_acceptance_ids = set(acceptance_ids)
        for entry in self.role_state_matrix:
            _require_references(entry.requirement_ids, valid_requirement_ids, "role/state requirement")
            _require_references(
                entry.acceptance_criterion_ids,
                valid_acceptance_ids,
                "role/state acceptance criterion",
            )

        repositories = self.affected_surface.repositories
        normalized_repositories = [
            _normalize_repository_identity(repository) for repository in repositories
        ]
        _require_unique(repositories, "affected repository")
        baseline_repositories = [
            baseline.repository for baseline in self.baseline_repository_shas
        ]
        normalized_baseline_repositories = [
            _normalize_repository_identity(repository)
            for repository in baseline_repositories
        ]
        _require_unique(baseline_repositories, "baseline repository")
        _require_exact_members(
            normalized_baseline_repositories,
            normalized_repositories,
            "baseline repository SHAs must cover exactly the affected repositories",
        )
        repository_set = set(normalized_repositories)
        impacts = (
            self.affected_surface.files
            + self.affected_surface.apis
            + self.affected_surface.schemas
            + self.affected_surface.migrations
            + self.affected_surface.translations
        )
        for impact in impacts:
            if _normalize_repository_identity(impact.repository) not in repository_set:
                raise ValueError(
                    f"affected surface {impact.target!r} references undeclared repository {impact.repository!r}"
                )
        for precedent in self.existing_precedents:
            if _normalize_repository_identity(precedent.repository) not in repository_set:
                raise ValueError(
                    f"existing precedent {precedent.path!r} references undeclared "
                    f"repository {precedent.repository!r}"
                )
            precedent_path = PurePosixPath(precedent.path.replace("\\", "/"))
            if precedent_path.is_absolute() or ".." in precedent_path.parts:
                raise ValueError(
                    f"existing precedent path {precedent.path!r} must stay within "
                    f"repository {precedent.repository!r}"
                )

        if self.epic_strategy and self.epic_strategy.mode == "decomposed":
            _require_unique(
                [child.id for child in self.epic_strategy.bounded_child_plans],
                "child-plan ID",
            )
            _require_unique(
                [child.issue_key for child in self.epic_strategy.bounded_child_plans],
                "child-plan Jira issue key",
            )
            child_requirement_ids = [
                requirement_id
                for child in self.epic_strategy.bounded_child_plans
                for requirement_id in child.requirement_ids
            ]
            child_acceptance_ids = [
                acceptance_id
                for child in self.epic_strategy.bounded_child_plans
                for acceptance_id in child.acceptance_criterion_ids
            ]
            _require_unique(child_requirement_ids, "child-plan requirement assignment")
            _require_unique(child_acceptance_ids, "child-plan acceptance-criterion assignment")
            _require_exact_members(
                child_requirement_ids,
                requirement_ids,
                "decomposed child plans must partition all requirements",
            )
            _require_exact_members(
                child_acceptance_ids,
                acceptance_ids,
                "decomposed child plans must partition all acceptance criteria",
            )
            requirement_child = {
                requirement_id: child.id
                for child in self.epic_strategy.bounded_child_plans
                for requirement_id in child.requirement_ids
            }
            acceptance_child = {
                acceptance_id: child.id
                for child in self.epic_strategy.bounded_child_plans
                for acceptance_id in child.acceptance_criterion_ids
            }
            misplaced_acceptance = sorted(
                criterion.id
                for requirement in self.requirements
                for criterion in requirement.acceptance_criteria
                if acceptance_child[criterion.id] != requirement_child[requirement.id]
            )
            if misplaced_acceptance:
                raise ValueError(
                    "decomposed child plans must keep acceptance criteria with their "
                    f"owning requirements: {', '.join(misplaced_acceptance)}"
                )
        return self

    def canonical_json(self, *, indent: int | None = None) -> str:
        if indent is not None:
            return json.dumps(
                self.model_dump(mode="json", exclude_none=False),
                ensure_ascii=False,
                indent=indent,
                sort_keys=True,
            )
        return json.dumps(
            self.model_dump(mode="json", exclude_none=False),
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


def parse_plan_spec(
    message: str,
    *,
    expected_issue_key: str | None = None,
    expected_snapshot_hash: str | None = None,
    issue_type: str | None = None,
    requirements_snapshot: RequirementsSnapshot | None = None,
) -> PlanSpec:
    """Parse JSON (including fenced JSON) and validate its Jira/repository traceability."""
    if not message or not message.strip():
        raise PlanSpecError("PlanSpec output is empty")

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
        if str(payload.get("decision") or "").strip().lower() in {
            "needs_human",
            "needs human",
            "human_required",
            "requires_human",
        }:
            question = str(payload.get("question") or payload.get("reason") or "").strip()
            suffix = f": {question}" if question else ""
            raise PlanSpecError(f"planning response requests human clarification{suffix}")
        try:
            plan = PlanSpec.model_validate(payload)
        except ValidationError as exc:
            validation_errors.append(_format_validation_error(exc))
            continue
        validate_plan_spec_context(
            plan,
            expected_issue_key=expected_issue_key,
            expected_snapshot_hash=expected_snapshot_hash,
            issue_type=issue_type,
            requirements_snapshot=requirements_snapshot,
        )
        return plan

    if validation_errors:
        raise PlanSpecError(f"invalid PlanSpec: {validation_errors[0]}")
    if saw_object:
        raise PlanSpecError("planning output contains JSON, but not a valid PlanSpec object")
    raise PlanSpecError("planning output must contain one complete JSON PlanSpec object")


def parse_frozen_legacy_plan_spec(
    message: str,
    *,
    expected_issue_key: str,
    expected_snapshot_hash: str,
    issue_type: str | None,
    requirements_snapshot: RequirementsSnapshot,
) -> PlanSpec:
    """Validate an exact pre-v4 PlanSpec without promoting contextual sources.

    Historical plans may contain context-only citations that were valid under
    the old schema. They are accepted here only as non-authoritative extras:
    every requirement and acceptance criterion must still retain a citation to
    root Description, configured Acceptance Criteria, or a root comment after
    those extras are removed. The original model is returned so its frozen
    content hash remains unchanged.
    """

    if requirements_snapshot.schema_version not in _LEGACY_REQUIREMENTS_SCHEMA_VERSIONS:
        raise PlanSpecError(
            "legacy frozen PlanSpec validation requires a v1-v3 requirements snapshot"
        )
    if requirements_snapshot.calculate_content_hash() != expected_snapshot_hash:
        raise PlanSpecError(
            "legacy frozen requirements snapshot does not match its trusted hash"
        )

    plan = _parse_plan_spec_model(message)
    authoritative_source_keys = _snapshot_source_keys(requirements_snapshot)

    def authoritative_sources(sources: list[JiraSource]) -> list[JiraSource]:
        return [
            source
            for source in sources
            if (source.issue_key, source.source_type, source.source_id)
            in authoritative_source_keys
        ]

    projected_requirements: list[PlanRequirement] = []
    missing_authority: list[str] = []
    for requirement in plan.requirements:
        requirement_sources = authoritative_sources(requirement.jira_sources)
        if not requirement_sources:
            missing_authority.append(f"requirement {requirement.id}")
        projected_criteria: list[AcceptanceCriterion] = []
        for criterion in requirement.acceptance_criteria:
            criterion_sources = authoritative_sources(criterion.jira_sources)
            if not criterion_sources:
                missing_authority.append(
                    f"acceptance criterion {criterion.id}"
                )
            projected_criteria.append(
                criterion.model_copy(update={"jira_sources": criterion_sources})
            )
        projected_requirements.append(
            requirement.model_copy(
                update={
                    "jira_sources": requirement_sources,
                    "acceptance_criteria": projected_criteria,
                }
            )
        )
    if missing_authority:
        raise PlanSpecError(
            "Frozen pre-v4 PlanSpec depends on non-authoritative Jira context; "
            "return to planning: " + ", ".join(missing_authority)
        )

    projected_questions = [
        question.model_copy(
            update={"jira_sources": authoritative_sources(question.jira_sources)}
        )
        for question in plan.open_questions
    ]
    projected_plan = plan.model_copy(
        update={
            "requirements": projected_requirements,
            "open_questions": projected_questions,
        }
    )
    validate_plan_spec_context(
        projected_plan,
        expected_issue_key=expected_issue_key,
        expected_snapshot_hash=expected_snapshot_hash,
        issue_type=issue_type,
        requirements_snapshot=requirements_snapshot,
    )
    return plan


def _parse_plan_spec_model(message: str) -> PlanSpec:
    """Parse only the stable PlanSpec model; context is validated by the caller."""

    if not message or not message.strip():
        raise PlanSpecError("PlanSpec output is empty")
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
        try:
            return PlanSpec.model_validate(payload)
        except ValidationError as exc:
            validation_errors.append(_format_validation_error(exc))
    if validation_errors:
        raise PlanSpecError(f"invalid PlanSpec: {validation_errors[0]}")
    if saw_object:
        raise PlanSpecError("planning output contains JSON, but not a valid PlanSpec object")
    raise PlanSpecError("planning output must contain one complete JSON PlanSpec object")


def validate_plan_spec_context(
    plan: PlanSpec,
    *,
    expected_issue_key: str | None = None,
    expected_snapshot_hash: str | None = None,
    issue_type: str | None = None,
    requirements_snapshot: RequirementsSnapshot | None = None,
) -> None:
    if (
        requirements_snapshot is None
        or requirements_snapshot.schema_version
        not in _LEGACY_REQUIREMENTS_SCHEMA_VERSIONS
    ):
        _normalize_current_plan_repository_identities(plan)
    unsupported_sources = sorted(
        {
            source.source_type
            for source in _plan_sources(plan)
            if source.source_type not in _ACTIVE_JIRA_SOURCE_TYPES
        }
    )
    if unsupported_sources:
        raise PlanSpecError(
            "Current PlanSpec Jira sources must be root description, "
            "custom_field, or comment evidence; unsupported source type(s): "
            + ", ".join(unsupported_sources)
        )
    if expected_issue_key and plan.issue_key != expected_issue_key:
        raise PlanSpecError(
            f"PlanSpec issue_key {plan.issue_key!r} does not match Jira issue {expected_issue_key!r}"
        )
    if expected_snapshot_hash and plan.requirements_snapshot_hash != expected_snapshot_hash:
        raise PlanSpecError(
            "PlanSpec requirements_snapshot_hash does not match the current requirements snapshot"
        )
    if (issue_type or "").strip().lower() == "epic" and plan.epic_strategy is None:
        raise PlanSpecError(
            "Epic PlanSpec must decompose work into bounded child plans or require explicit single-change approval"
        )
    if requirements_snapshot is not None:
        valid_sources = _snapshot_source_keys(requirements_snapshot)
        invalid_sources = sorted(
            {
                (source.issue_key, source.source_type, source.source_id)
                for source in _plan_sources(plan)
                if (source.issue_key, source.source_type, source.source_id)
                not in valid_sources
            }
        )
        if invalid_sources:
            formatted = ", ".join(
                f"{issue_key}:{source_type}:{source_id}"
                for issue_key, source_type, source_id in invalid_sources
            )
            raise PlanSpecError(f"PlanSpec references Jira sources absent from the current snapshot: {formatted}")
        _validate_current_decision_coverage(plan, requirements_snapshot)
        _validate_epic_child_issue_keys(plan, requirements_snapshot)


def validate_plan_precedent_paths(
    plan: PlanSpec,
    workspace_path: Path,
) -> str | None:
    """Verify baselined precedent evidence exists inside its declared repository."""

    workspace_root = workspace_path.resolve()
    for precedent in plan.existing_precedents:
        repository_path = (workspace_root / precedent.repository).resolve()
        try:
            repository_path.relative_to(workspace_root)
        except ValueError:
            return (
                f"PlanSpec precedent repository {precedent.repository!r} resolves outside "
                f"the workspace at {repository_path}."
            )
        if not repository_path.is_dir():
            return (
                f"PlanSpec precedent repository {precedent.repository!r} is missing or not "
                f"a directory at {repository_path}."
            )

        precedent_path = (repository_path / precedent.path).resolve()
        try:
            precedent_path.relative_to(repository_path)
        except ValueError:
            return (
                f"PlanSpec precedent path {precedent.path!r} resolves outside repository "
                f"{precedent.repository!r} at {precedent_path}."
            )
        if not precedent_path.exists():
            return (
                f"PlanSpec precedent path {precedent.path!r} does not exist in repository "
                f"{precedent.repository!r} at {precedent_path}."
            )
    return None


def plan_spec_hash(message: str, **context: Any) -> str:
    return parse_plan_spec(message, **context).content_hash()


def plan_spec_json_schema() -> str:
    return json.dumps(PlanSpec.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True)


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
    return [candidate for candidate in candidates if not (candidate in seen or seen.add(candidate))]


def _format_validation_error(error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"])
        details.append(f"{location or 'root'}: {item['msg']}")
    return "; ".join(details)


def _plan_sources(plan: PlanSpec) -> list[JiraSource]:
    sources = [source for requirement in plan.requirements for source in requirement.jira_sources]
    sources.extend(
        source
        for requirement in plan.requirements
        for criterion in requirement.acceptance_criteria
        for source in criterion.jira_sources
    )
    sources.extend(source for question in plan.open_questions for source in question.jira_sources)
    return sources


def _validate_epic_child_issue_keys(
    plan: PlanSpec,
    snapshot: RequirementsSnapshot,
) -> None:
    strategy = plan.epic_strategy
    if strategy is None or strategy.mode != "decomposed":
        return

    available_issue_keys = {
        related.identifier
        for related in snapshot.children + snapshot.linked_issues
        if related.identifier
    }
    unknown_issue_keys = sorted(
        child.issue_key
        for child in strategy.bounded_child_plans
        if child.issue_key not in available_issue_keys
    )
    if unknown_issue_keys:
        raise PlanSpecError(
            "Decomposed Epic child plans must name Jira issues present in the "
            "requirements snapshot's child or linked issues: "
            + ", ".join(unknown_issue_keys)
        )


def _snapshot_source_keys(snapshot: RequirementsSnapshot) -> set[tuple[str, str, str]]:
    root_issue = snapshot.issue_identifier
    root_sources = []
    if snapshot.description is not None and snapshot.description.planning_eligible:
        root_sources.append(snapshot.description.source)
    root_sources.extend(
        artifact.source
        for artifact in snapshot.custom_fields
        if artifact.planning_eligible and artifact.kind == "acceptance_criterion"
    )
    root_sources.extend(
        artifact.source
        for artifact in snapshot.comments
        if artifact.planning_eligible
    )
    base_keys = {
        (source.issue_identifier, source.source_type, source.source_id)
        for source in root_sources
        if source.issue_identifier == root_issue
        and source.source_type in _ACTIVE_JIRA_SOURCE_TYPES
    }
    keys = set(base_keys)

    def is_allowed_decision_source(source: Any) -> bool:
        return source.issue_identifier == root_issue and any(
            source.source_type == source_type
            and (
                source.source_id == source_id
                or source.source_id.startswith(f"{source_id}#unit:")
            )
            for _, source_type, source_id in base_keys
        )

    for decision in (
        snapshot.current_requirements
        + snapshot.superseded_requirements
        + snapshot.inferred_behavior
        + snapshot.unresolved_contradictions
    ):
        for source in decision.sources:
            if is_allowed_decision_source(source):
                keys.add(
                    (
                        source.issue_identifier,
                        source.source_type,
                        source.source_id,
                    )
                )
    return keys


def _validate_current_decision_coverage(
    plan: PlanSpec,
    snapshot: RequirementsSnapshot,
) -> None:
    """Require active plan rows to cover current decisions in their matching layer."""

    allowed_source_keys = _snapshot_source_keys(snapshot)
    allow_base_artifact_citations = (
        snapshot.schema_version in _LEGACY_REQUIREMENTS_SCHEMA_VERSIONS
    )

    requirement_sources = {
        (source.issue_key, source.source_type, source.source_id)
        for requirement in plan.requirements
        for source in requirement.jira_sources
    }
    acceptance_sources = {
        (source.issue_key, source.source_type, source.source_id)
        for requirement in plan.requirements
        for criterion in requirement.acceptance_criteria
        for source in criterion.jira_sources
    }

    current_source_keys = {
        (source.issue_identifier, source.source_type, source.source_id)
        for decision in snapshot.current_requirements
        if decision.kind != "supporting_evidence"
        for source in decision.sources
        if (source.issue_identifier, source.source_type, source.source_id)
        in allowed_source_keys
    }
    non_current_sources: dict[tuple[str, str, str], set[str]] = {}
    for classification, decisions in (
        ("superseded", snapshot.superseded_requirements),
        ("inferred", snapshot.inferred_behavior),
        ("unresolved contradiction", snapshot.unresolved_contradictions),
    ):
        for decision in decisions:
            for source in decision.sources:
                if (
                    source.issue_identifier,
                    source.source_type,
                    source.source_id,
                ) not in allowed_source_keys:
                    continue
                key = (
                    source.issue_identifier,
                    source.source_type,
                    source.source_id,
                )
                non_current_sources.setdefault(key, set()).add(classification)

    promoted_sources = sorted(
        source_key
        for source_key in requirement_sources | acceptance_sources
        if any(
            _source_keys_match_decision(
                source_key,
                non_current_key,
                allow_base_artifact_citations=allow_base_artifact_citations,
            )
            for non_current_key in non_current_sources
        )
        and not any(
            _source_keys_match_decision(
                source_key,
                current_key,
                allow_base_artifact_citations=allow_base_artifact_citations,
            )
            for current_key in current_source_keys
        )
    )
    if promoted_sources:
        formatted_sources: list[str] = []
        for issue_key, source_type, source_id in promoted_sources:
            classifications = _classifications_for_source(
                (issue_key, source_type, source_id),
                non_current_sources,
                allow_base_artifact_citations=allow_base_artifact_citations,
            )
            formatted_sources.append(
                f"{issue_key}:{source_type}:{source_id} "
                f"({'/'.join(sorted(classifications))})"
            )
        formatted = ", ".join(formatted_sources)
        raise PlanSpecError(
            "PlanSpec active requirements or acceptance criteria cite non-current Jira "
            f"decision sources: {formatted}"
        )

    uncovered: list[str] = []
    for decision in snapshot.current_requirements:
        if decision.kind == "supporting_evidence":
            continue
        decision_sources = {
            (source.issue_identifier, source.source_type, source.source_id)
            for source in decision.sources
            if (source.issue_identifier, source.source_type, source.source_id)
            in allowed_source_keys
        }
        if not decision_sources:
            continue
        cited_sources = (
            requirement_sources
            if decision.kind == "requirement"
            else acceptance_sources
        )
        if not _source_sets_overlap(
            cited_sources,
            decision_sources,
            allow_base_artifact_citations=allow_base_artifact_citations,
        ):
            formatted_sources = ", ".join(
                f"{issue_key}:{source_type}:{source_id}"
                for issue_key, source_type, source_id in sorted(decision_sources)
            )
            uncovered.append(f"{decision.id} (cite one of {formatted_sources})")

    if uncovered:
        raise PlanSpecError(
            "PlanSpec does not cover every current Jira requirement or acceptance-criterion "
            f"decision: {'; '.join(uncovered)}"
        )

    current_requirement_sources = {
        (source.issue_identifier, source.source_type, source.source_id)
        for decision in snapshot.current_requirements
        if decision.kind == "requirement"
        for source in decision.sources
        if (source.issue_identifier, source.source_type, source.source_id)
        in allowed_source_keys
    }
    current_acceptance_sources = {
        (source.issue_identifier, source.source_type, source.source_id)
        for decision in snapshot.current_requirements
        if decision.kind == "acceptance_criterion"
        for source in decision.sources
        if (source.issue_identifier, source.source_type, source.source_id)
        in allowed_source_keys
    }

    def cites_current_decision(
        sources: list[JiraSource],
        matching_sources: set[tuple[str, str, str]],
    ) -> bool:
        source_keys = {
            (source.issue_key, source.source_type, source.source_id)
            for source in sources
        }
        return _source_sets_overlap(
            source_keys,
            matching_sources,
            allow_base_artifact_citations=allow_base_artifact_citations,
        )

    unanchored_requirements = [
        requirement.id
        for requirement in plan.requirements
        if not cites_current_decision(
            requirement.jira_sources,
            current_requirement_sources,
        )
    ]
    unanchored_acceptance_criteria = [
        criterion.id
        for requirement in plan.requirements
        for criterion in requirement.acceptance_criteria
        if not cites_current_decision(
            criterion.jira_sources,
            current_acceptance_sources,
        )
    ]
    if unanchored_requirements or unanchored_acceptance_criteria:
        details: list[str] = []
        if unanchored_requirements:
            details.append(f"requirements {', '.join(unanchored_requirements)}")
        if unanchored_acceptance_criteria:
            details.append(
                "acceptance criteria "
                + ", ".join(unanchored_acceptance_criteria)
            )
        raise PlanSpecError(
            "Every PlanRequirement and AcceptanceCriterion must cite a matching-layer "
            f"current authoritative Jira decision source: {'; '.join(details)}"
        )


def _source_keys_refer_to_same_artifact(
    left: tuple[str, str, str],
    right: tuple[str, str, str],
) -> bool:
    """Preserve pre-v4 base-artifact/unit citation equivalence."""

    if left[:2] != right[:2]:
        return False
    left_id = left[2]
    right_id = right[2]
    return (
        left_id == right_id
        or left_id.startswith(f"{right_id}#unit:")
        or right_id.startswith(f"{left_id}#unit:")
    )


def _source_keys_match_decision(
    left: tuple[str, str, str],
    right: tuple[str, str, str],
    *,
    allow_base_artifact_citations: bool,
) -> bool:
    if not allow_base_artifact_citations:
        return left == right
    return _source_keys_refer_to_same_artifact(left, right)


def _source_sets_overlap(
    left: set[tuple[str, str, str]],
    right: set[tuple[str, str, str]],
    *,
    allow_base_artifact_citations: bool,
) -> bool:
    return any(
        _source_keys_match_decision(
            left_key,
            right_key,
            allow_base_artifact_citations=allow_base_artifact_citations,
        )
        for left_key in left
        for right_key in right
    )


def _classifications_for_source(
    source_key: tuple[str, str, str],
    classified_sources: dict[tuple[str, str, str], set[str]],
    *,
    allow_base_artifact_citations: bool,
) -> set[str]:
    return {
        classification
        for classified_key, classifications in classified_sources.items()
        if _source_keys_match_decision(
            source_key,
            classified_key,
            allow_base_artifact_citations=allow_base_artifact_citations,
        )
        for classification in classifications
    }


def _normalize_repository_identity(value: str) -> str:
    """Return one stable POSIX identity for a workspace-relative repository."""

    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"repository {value!r} must be a workspace-relative path without '..'"
        )
    return path.as_posix()


def _normalize_current_plan_repository_identities(plan: PlanSpec) -> None:
    """Canonicalize current PlanSpec repository names and reject aliases."""

    repositories = plan.affected_surface.repositories
    normalized_repositories = [
        _normalize_repository_identity(repository) for repository in repositories
    ]
    _reject_repository_aliases(
        repositories,
        normalized_repositories,
        "affected repositories",
    )

    baseline_repositories = [
        baseline.repository for baseline in plan.baseline_repository_shas
    ]
    normalized_baseline_repositories = [
        _normalize_repository_identity(repository)
        for repository in baseline_repositories
    ]
    _reject_repository_aliases(
        baseline_repositories,
        normalized_baseline_repositories,
        "baseline repositories",
    )
    _require_exact_members(
        normalized_baseline_repositories,
        normalized_repositories,
        "baseline repository SHAs must cover exactly the affected repositories",
    )

    plan.affected_surface.repositories = normalized_repositories
    for baseline, normalized in zip(
        plan.baseline_repository_shas,
        normalized_baseline_repositories,
        strict=True,
    ):
        baseline.repository = normalized
    for impact in (
        plan.affected_surface.files
        + plan.affected_surface.apis
        + plan.affected_surface.schemas
        + plan.affected_surface.migrations
        + plan.affected_surface.translations
    ):
        impact.repository = _normalize_repository_identity(impact.repository)
    for precedent in plan.existing_precedents:
        precedent.repository = _normalize_repository_identity(precedent.repository)


def _reject_repository_aliases(
    raw_values: list[str],
    normalized_values: list[str],
    label: str,
) -> None:
    aliases: dict[str, list[str]] = {}
    for raw, normalized in zip(raw_values, normalized_values, strict=True):
        aliases.setdefault(normalized, []).append(raw)
    duplicates = {
        normalized: values
        for normalized, values in aliases.items()
        if len(values) > 1
    }
    if duplicates:
        details = "; ".join(
            f"{normalized!r}: {', '.join(repr(value) for value in values)}"
            for normalized, values in sorted(duplicates.items())
        )
        raise PlanSpecError(
            f"{label} contain aliases that resolve to the same workspace-relative "
            f"identity: {details}"
        )



def _require_unique(values: list[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}(s): {', '.join(duplicates)}")


def _require_references(values: list[str], valid: set[str], label: str) -> None:
    unknown = sorted(set(values) - valid)
    if unknown:
        raise ValueError(f"unknown {label} reference(s): {', '.join(unknown)}")


def _require_exact_members(actual: list[str], expected: list[str], label: str) -> None:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise ValueError(f"{label}: {'; '.join(details)}")
