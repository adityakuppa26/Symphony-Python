from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import RequirementsSnapshot


PLAN_SPEC_VERSION = "1.0"


_CANONICAL_ROLE_ORDER = ("gc", "sub", "gc_as_sub")
_CANONICAL_ROLE_LABELS = {
    "gc": "GC",
    "sub": "Sub",
    "gc_as_sub": "GC acting as Sub",
}
_GC_AS_SUB_PATTERN = re.compile(
    r"\b(?:gcs?|general[\s-]+contractors?)"
    r"[\s-]*(?:acting[\s-]+as|as)"
    r"[\s-]+(?:an?[\s-]+)?(?:subs?|subcontractors?)\b",
    re.IGNORECASE,
)
_GC_PATTERN = re.compile(
    r"\b(?:gcs?|general[\s-]+contractors?)\b",
    re.IGNORECASE,
)
_SUB_PATTERN = re.compile(
    r"\b(?:subs?|subcontractors?)\b",
    re.IGNORECASE,
)
_ROLE_LABEL_EXCLUSION_PATTERN = re.compile(
    r"(?:\b(?:non|not|except|excluding|exclude|without)\b|\ball\s+(?:users?\s+)?except\b)",
    re.IGNORECASE,
)
_ROLE_TOKEN_PATTERN_TEXT = (
    r"(?:"
    r"(?:gcs?|general[\s-]+contractors?)[\s-]*(?:acting[\s-]+as|as)"
    r"[\s-]+(?:an?[\s-]+)?(?:subs?|subcontractors?)"
    r"|gcs?|general[\s-]+contractors?|subs?|subcontractors?"
    r")"
)
_COORDINATED_ABSENT_ROLE_PATTERN = re.compile(
    rf"(?P<roles>{_ROLE_TOKEN_PATTERN_TEXT}"
    rf"(?:\s*(?:,|/|&|\band\b|\bor\b)\s*{_ROLE_TOKEN_PATTERN_TEXT})+)"
    r"\s*(?:(?:is|are)\s+)?"
    r"(?:not\s+(?:shown|visible|displayed|present|applicable)|"
    r"absent|missing|omitted|hidden)\b",
    re.IGNORECASE,
)
_NO_COORDINATED_ROLE_PATTERN = re.compile(
    rf"\bno\s+(?P<roles>{_ROLE_TOKEN_PATTERN_TEXT}"
    rf"(?:\s*(?:,|/|&|\band\b|\bor\b)\s*{_ROLE_TOKEN_PATTERN_TEXT})+)"
    r"\s+(?:(?:is|are)\s+)?(?:shown|visible|displayed|present|applicable)\b",
    re.IGNORECASE,
)
_ABSENT_ROLE_SUFFIX_PATTERN = re.compile(
    r"^\s*(?::|[-–—]|=)?\s*(?:(?:is|are)\s+)?"
    r"(?:not\s+(?:shown|visible|displayed|present|applicable)|"
    r"absent|missing|omitted|hidden)\b",
    re.IGNORECASE,
)
_ABSENT_ROLE_PREFIX_PATTERN = re.compile(
    r"(?:\bno|(?:not\s+(?:shown|visible|displayed|present)|absent|missing)"
    r"(?:\s+for)?)\s*$",
    re.IGNORECASE,
)




class PlanSpecError(ValueError):
    """Raised when a planning response is not a valid, context-bound PlanSpec."""


class StrictPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class JiraSource(StrictPlanModel):
    issue_key: str = Field(min_length=1)
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
            raise ValueError("a role/state entry must reference a requirement or acceptance criterion")
        detected_roles = _detect_canonical_roles(self.role)
        if self.canonical_role in _CANONICAL_ROLE_ORDER:
            if _ROLE_LABEL_EXCLUSION_PATTERN.search(self.role):
                raise ValueError(
                    "a role-specific matrix row cannot use a negated or exclusionary role label"
                )
            if detected_roles != {self.canonical_role}:
                expected = _CANONICAL_ROLE_LABELS[self.canonical_role]
                raise ValueError(
                    f"role label must identify only its canonical_role {expected!r}"
                )
        elif detected_roles:
            raise ValueError(
                "an all/other matrix row cannot use a GC, Sub, or GC-as-Sub role label"
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
    role_state_matrix: list[RoleStateMatrixEntry] = Field(min_length=1)
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
        _require_unique(mapped_acceptance_ids, "test acceptance-criterion mapping")
        _require_exact_members(
            mapped_acceptance_ids,
            acceptance_ids,
            "test cases must map one-to-one to acceptance criteria",
        )

        valid_requirement_ids = set(requirement_ids)
        valid_acceptance_ids = set(acceptance_ids)
        matrix_requirement_ids: list[str] = []
        matrix_acceptance_ids: list[str] = []
        for entry in self.role_state_matrix:
            _require_references(entry.requirement_ids, valid_requirement_ids, "role/state requirement")
            _require_references(
                entry.acceptance_criterion_ids,
                valid_acceptance_ids,
                "role/state acceptance criterion",
            )
            matrix_requirement_ids.extend(entry.requirement_ids)
            matrix_acceptance_ids.extend(entry.acceptance_criterion_ids)
        _require_exact_members(
            matrix_requirement_ids,
            requirement_ids,
            "role/state matrix must reference every requirement",
        )
        _require_exact_members(
            matrix_acceptance_ids,
            acceptance_ids,
            "role/state matrix must reference every acceptance criterion",
        )

        repositories = self.affected_surface.repositories
        _require_unique(repositories, "affected repository")
        baseline_repositories = [baseline.repository for baseline in self.baseline_repository_shas]
        _require_unique(baseline_repositories, "baseline repository")
        _require_exact_members(
            baseline_repositories,
            repositories,
            "baseline repository SHAs must cover exactly the affected repositories",
        )
        repository_set = set(repositories)
        impacts = (
            self.affected_surface.files
            + self.affected_surface.apis
            + self.affected_surface.schemas
            + self.affected_surface.migrations
            + self.affected_surface.translations
        )
        for impact in impacts:
            if impact.repository not in repository_set:
                raise ValueError(
                    f"affected surface {impact.target!r} references undeclared repository {impact.repository!r}"
                )
        for precedent in self.existing_precedents:
            if precedent.repository not in repository_set:
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


def validate_plan_spec_context(
    plan: PlanSpec,
    *,
    expected_issue_key: str | None = None,
    expected_snapshot_hash: str | None = None,
    issue_type: str | None = None,
    requirements_snapshot: RequirementsSnapshot | None = None,
) -> None:
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
        _validate_complete_attachment_coverage(plan, requirements_snapshot)
        _validate_required_role_matrix(plan, requirements_snapshot)
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


def _active_plan_source_keys(plan: PlanSpec) -> set[tuple[str, str, str]]:
    keys = {
        (source.issue_key, source.source_type, source.source_id)
        for requirement in plan.requirements
        for source in requirement.jira_sources
    }
    keys.update(
        (source.issue_key, source.source_type, source.source_id)
        for requirement in plan.requirements
        for criterion in requirement.acceptance_criteria
        for source in criterion.jira_sources
    )
    return keys


def _complete_snapshot_attachments(snapshot: RequirementsSnapshot) -> list[Any]:
    attachments = list(snapshot.attachments)
    related_issues = (
        ([snapshot.parent] if snapshot.parent is not None else [])
        + snapshot.children
        + snapshot.linked_issues
        + snapshot.dependencies
    )
    for related in related_issues:
        attachments.extend(related.attachments)
    return [
        attachment
        for attachment in attachments
        if attachment.analysis.status == "complete"
    ]


def _validate_complete_attachment_coverage(
    plan: PlanSpec,
    snapshot: RequirementsSnapshot,
) -> None:
    required_sources = {
        (
            attachment.source.issue_identifier,
            "attachment",
            attachment.source.source_id,
        )
        for attachment in _complete_snapshot_attachments(snapshot)
    }
    active_sources = _active_plan_source_keys(plan)
    missing_sources = sorted(
        required_source
        for required_source in required_sources
        if not any(
            active_source[:2] == required_source[:2]
            and (
                active_source[2] == required_source[2]
                or active_source[2].startswith(f"{required_source[2]}#unit:")
            )
            for active_source in active_sources
        )
    )
    if missing_sources:
        formatted = ", ".join(
            f"{issue_key}:{source_type}:{source_id}"
            for issue_key, source_type, source_id in missing_sources
        )
        raise PlanSpecError(
            "PlanSpec active requirements or acceptance criteria must cite every "
            f"completely analyzed attachment: {formatted}"
        )


def _detect_canonical_roles(text: str) -> set[str]:
    roles: set[str] = set()
    if _GC_AS_SUB_PATTERN.search(text):
        roles.add("gc_as_sub")
    without_gc_as_sub = _GC_AS_SUB_PATTERN.sub(" ", text)
    if _GC_PATTERN.search(without_gc_as_sub):
        roles.add("gc")
    if _SUB_PATTERN.search(without_gc_as_sub):
        roles.add("sub")
    return roles


def _role_mention_is_absent(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 48) : match.start()]
    after = text[match.end() : match.end() + 64]
    if bool(
        _ABSENT_ROLE_PREFIX_PATTERN.search(before)
        or _ABSENT_ROLE_SUFFIX_PATTERN.match(after)
    ):
        return True
    return any(
        group.start("roles") <= match.start()
        and match.end() <= group.end("roles")
        for pattern in (
            _COORDINATED_ABSENT_ROLE_PATTERN,
            _NO_COORDINATED_ROLE_PATTERN,
        )
        for group in pattern.finditer(text)
    )


def _has_present_role_mention(text: str, pattern: re.Pattern[str]) -> bool:
    return any(
        not _role_mention_is_absent(text, match)
        for match in pattern.finditer(text)
    )


def _detect_attachment_summary_roles(text: str) -> set[str]:
    roles: set[str] = set()
    if _has_present_role_mention(text, _GC_AS_SUB_PATTERN):
        roles.add("gc_as_sub")
    without_gc_as_sub = _GC_AS_SUB_PATTERN.sub(
        lambda match: " " * len(match.group(0)),
        text,
    )
    if _has_present_role_mention(without_gc_as_sub, _GC_PATTERN):
        roles.add("gc")
    if _has_present_role_mention(without_gc_as_sub, _SUB_PATTERN):
        roles.add("sub")
    return roles


def _active_plan_citations(
    plan: PlanSpec,
) -> dict[tuple[str, str, str], dict[str, set[str]]]:
    citations: dict[tuple[str, str, str], dict[str, set[str]]] = {}

    def add(source: JiraSource, layer: str, item_id: str) -> None:
        key = (source.issue_key, source.source_type, source.source_id)
        citations.setdefault(
            key,
            {"requirement": set(), "acceptance_criterion": set()},
        )[layer].add(item_id)

    for requirement in plan.requirements:
        for source in requirement.jira_sources:
            add(source, "requirement", requirement.id)
        for criterion in requirement.acceptance_criteria:
            for source in criterion.jira_sources:
                add(source, "acceptance_criterion", criterion.id)
    return citations


def _validate_required_role_matrix(
    plan: PlanSpec,
    snapshot: RequirementsSnapshot,
) -> None:
    citations = _active_plan_citations(plan)
    entries_by_role: dict[str, list[RoleStateMatrixEntry]] = {
        role: [] for role in _CANONICAL_ROLE_ORDER
    }
    for entry in plan.role_state_matrix:
        if entry.canonical_role in entries_by_role:
            entries_by_role[entry.canonical_role].append(entry)

    missing: list[str] = []
    missing_roles: set[str] = set()

    def require_role_bindings(
        *,
        source_key: tuple[str, str, str],
        roles: set[str],
        layers: tuple[str, ...],
    ) -> None:
        source_citations = citations.get(source_key)
        if source_citations is None:
            return
        for role in _CANONICAL_ROLE_ORDER:
            if role not in roles:
                continue
            matching_entries = entries_by_role[role]
            for layer in layers:
                id_field = (
                    "requirement_ids"
                    if layer == "requirement"
                    else "acceptance_criterion_ids"
                )
                represented_ids = {
                    item_id
                    for entry in matching_entries
                    for item_id in getattr(entry, id_field)
                }
                missing_ids = sorted(source_citations[layer] - represented_ids)
                if missing_ids:
                    missing_roles.add(role)
                    issue_key, source_type, source_id = source_key
                    missing.append(
                        f"{issue_key}:{source_type}:{source_id} requires "
                        f"{_CANONICAL_ROLE_LABELS[role]} {layer} ID(s) "
                        f"{', '.join(missing_ids)} (canonical Jira role: "
                        f"{_CANONICAL_ROLE_LABELS[role]})"
                    )

    for decision in snapshot.current_requirements:
        roles = _detect_canonical_roles(decision.text)
        if not roles:
            continue
        if decision.kind == "requirement":
            layers = ("requirement",)
        elif decision.kind == "acceptance_criterion":
            layers = ("acceptance_criterion",)
        else:
            layers = ("requirement", "acceptance_criterion")
        for source in decision.sources:
            require_role_bindings(
                source_key=(
                    source.issue_identifier,
                    source.source_type,
                    source.source_id,
                ),
                roles=roles,
                layers=layers,
            )

    for attachment in _complete_snapshot_attachments(snapshot):
        roles = _detect_attachment_summary_roles(attachment.analysis.summary)
        if not roles:
            continue
        require_role_bindings(
            source_key=(
                attachment.source.issue_identifier,
                "attachment",
                attachment.source.source_id,
            ),
            roles=roles,
            layers=("requirement", "acceptance_criterion"),
        )

    if missing:
        role_summary = ", ".join(
            _CANONICAL_ROLE_LABELS[role]
            for role in _CANONICAL_ROLE_ORDER
            if role in missing_roles
        )
        raise PlanSpecError(
            "PlanSpec role_state_matrix must contain distinct entries for each "
            f"source-citing ID. Required canonical Jira roles: {role_summary}. "
            f"Details: {'; '.join(missing)}"
        )


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
    keys: set[tuple[str, str, str]] = set()

    def add_source(source: Any, source_type: str) -> None:
        if source is not None:
            keys.add((source.issue_identifier, source_type, source.source_id))

    def add_direct_source(source: Any) -> None:
        if source is not None and source.source_type != "relation":
            add_source(source, source.source_type)

    def add_related_issue(relation: Any, relationship_type: str) -> None:
        add_source(relation.source, relationship_type)
        for artifact in relation.requirements:
            add_source(artifact.source, artifact.source_type)
        for comment in relation.comments:
            add_source(comment.source, "comment")
        for attachment in relation.attachments:
            add_source(attachment.source, "attachment")

    if snapshot.description:
        add_source(snapshot.description.source, "description")
    for artifact in snapshot.custom_fields:
        add_source(artifact.source, "custom_field")
    for artifact in snapshot.comments:
        add_source(artifact.source, "comment")
    for attachment in snapshot.attachments:
        add_source(attachment.source, "attachment")
    if snapshot.parent:
        add_related_issue(snapshot.parent, "parent")
    for relation in snapshot.children:
        add_related_issue(relation, "child")
    for relation in snapshot.linked_issues:
        add_related_issue(relation, "linked_issue")
    for relation in snapshot.dependencies:
        add_related_issue(relation, "dependency")
    for decision in (
        snapshot.current_requirements
        + snapshot.superseded_requirements
        + snapshot.inferred_behavior
        + snapshot.unresolved_contradictions
    ):
        for source in decision.sources:
            add_direct_source(source)
    for value in snapshot.components:
        keys.add((snapshot.issue_identifier, "component", value.id or value.name))
    for value in snapshot.versions:
        keys.add((snapshot.issue_identifier, "version", value.id or value.name))
    return keys


def _validate_current_decision_coverage(
    plan: PlanSpec,
    snapshot: RequirementsSnapshot,
) -> None:
    """Require active plan rows to cover current decisions in their matching layer."""

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
        for source in decision.sources
    }
    non_current_sources: dict[tuple[str, str, str], set[str]] = {}
    for classification, decisions in (
        ("superseded", snapshot.superseded_requirements),
        ("inferred", snapshot.inferred_behavior),
        ("unresolved contradiction", snapshot.unresolved_contradictions),
    ):
        for decision in decisions:
            for source in decision.sources:
                key = (
                    source.issue_identifier,
                    source.source_type,
                    source.source_id,
                )
                non_current_sources.setdefault(key, set()).add(classification)

    promoted_sources = sorted(
        (requirement_sources | acceptance_sources)
        & (set(non_current_sources) - current_source_keys)
    )
    if promoted_sources:
        formatted = ", ".join(
            (
                f"{issue_key}:{source_type}:{source_id} "
                f"({'/'.join(sorted(non_current_sources[(issue_key, source_type, source_id)]))})"
            )
            for issue_key, source_type, source_id in promoted_sources
        )
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
        }
        cited_sources = (
            requirement_sources
            if decision.kind == "requirement"
            else acceptance_sources
        )
        missing_sources = sorted(decision_sources - cited_sources)
        if not decision_sources:
            uncovered.append(f"{decision.id} (no Jira sources)")
        elif missing_sources:
            formatted_sources = ", ".join(
                f"{issue_key}:{source_type}:{source_id}"
                for issue_key, source_type, source_id in missing_sources
            )
            uncovered.append(f"{decision.id} (missing {formatted_sources})")

    if uncovered:
        raise PlanSpecError(
            "PlanSpec does not cover every current Jira requirement or acceptance-criterion "
            f"decision: {'; '.join(uncovered)}"
        )

    current_requirement_sources = {
        (source.issue_identifier, source.source_type, source.source_id)
        for decision in snapshot.current_requirements
        for source in decision.sources
        if decision.kind == "requirement"
        or (
            decision.kind == "supporting_evidence"
            and source.source_type == "attachment"
        )
    }
    current_acceptance_sources = {
        (source.issue_identifier, source.source_type, source.source_id)
        for decision in snapshot.current_requirements
        if decision.kind == "acceptance_criterion"
        for source in decision.sources
    }

    def cites_current_decision(
        sources: list[JiraSource],
        matching_sources: set[tuple[str, str, str]],
    ) -> bool:
        source_keys = {
            (source.issue_key, source.source_type, source.source_id)
            for source in sources
        }
        return bool(source_keys & matching_sources)

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
            "Every PlanRequirement and AcceptanceCriterion must cite a matching-layer current "
            f"Jira decision source: {'; '.join(details)}"
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
