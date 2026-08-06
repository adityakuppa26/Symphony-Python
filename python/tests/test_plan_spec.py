from __future__ import annotations

import json

import pytest

from symphony_jira.models import (
    AttachmentAnalysis,
    IssueAttachment,
    IssueComment,
    RelatedIssue,
    RequirementArtifact,
    RequirementDecision,
    RequirementSource,
    RequirementsSnapshot,
)
from symphony_jira.plan_spec import (
    PlanSpecError,
    parse_frozen_legacy_plan_spec,
    parse_plan_spec,
    validate_plan_precedent_paths,
)


SNAPSHOT_HASH = "b" * 64


def test_fenced_plan_spec_parses_and_hash_is_canonical() -> None:
    payload = plan_payload()
    fenced = f"Plan follows.\n```json\n{json.dumps(payload, indent=2)}\n```"
    reordered = json.dumps(dict(reversed(list(payload.items()))))

    first = parse_plan_spec(
        fenced,
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=SNAPSHOT_HASH,
    )
    second = parse_plan_spec(reordered)

    assert first.content_hash() == second.content_hash()
    assert json.loads(first.canonical_json())["schema_version"] == "1.0"


def test_test_cases_must_cover_known_acceptance_criteria() -> None:
    payload = plan_payload()
    payload["test_cases"][0]["acceptance_criterion_id"] = "AC-unknown"

    with pytest.raises(PlanSpecError, match="cover every acceptance criterion"):
        parse_plan_spec(json.dumps(payload))


def test_multiple_test_cases_can_cover_one_acceptance_criterion() -> None:
    payload = plan_payload()
    payload["test_cases"].append(
        {
            "id": "TC-gc-integration",
            "acceptance_criterion_id": "AC-gc",
            "level": "integration",
            "description": "Load the table with the GC API response.",
            "expected_result": "The GC-specific value is visible.",
        }
    )

    plan = parse_plan_spec(json.dumps(payload))

    assert [test.acceptance_criterion_id for test in plan.test_cases] == [
        "AC-gc",
        "AC-gc",
    ]


def test_all_jira_sources_must_exist_in_current_snapshot() -> None:
    snapshot = requirements_snapshot()
    payload = plan_payload(snapshot_hash=snapshot.calculate_content_hash())
    payload["requirements"][0]["jira_sources"][0]["source_id"] = "missing-comment"

    with pytest.raises(PlanSpecError, match="absent from the current snapshot"):
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.calculate_content_hash(),
            requirements_snapshot=snapshot,
        )


def test_jira_source_type_must_match_snapshot_catalog() -> None:
    snapshot = requirements_snapshot()
    snapshot_hash = snapshot.calculate_content_hash()
    payload = plan_payload(snapshot_hash=snapshot_hash)
    payload["requirements"][0]["jira_sources"][0]["source_type"] = "comment"

    with pytest.raises(PlanSpecError, match="absent from the current snapshot") as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot_hash,
            requirements_snapshot=snapshot,
        )

    assert "ICPM-67703:comment:description" in str(error.value)


@pytest.mark.parametrize(
    "source_type",
    [
        "attachment",
        "parent",
        "child",
        "linked_issue",
        "dependency",
        "component",
        "version",
    ],
)
def test_plan_sources_are_limited_to_jira_requirement_content(
    source_type: str,
) -> None:
    payload = plan_payload()
    payload["requirements"][0]["jira_sources"].append(
        {
            "issue_key": "ICPM-67703",
            "source_type": source_type,
            "source_id": f"context:{source_type}",
        }
    )

    with pytest.raises(PlanSpecError, match="description.*custom_field.*comment"):
        parse_plan_spec(json.dumps(payload))


@pytest.mark.parametrize(
    "source_type",
    [
        "attachment",
        "parent",
        "child",
        "linked_issue",
        "dependency",
        "component",
        "version",
    ],
)
def test_frozen_legacy_plan_parses_context_only_source_types(
    source_type: str,
) -> None:
    snapshot = requirements_snapshot().model_copy(
        update={"schema_version": "jira-requirements/v3"}
    )
    snapshot_hash = snapshot.calculate_content_hash()
    payload = plan_payload(snapshot_hash=snapshot_hash)
    legacy_source = {
        "issue_key": "ICPM-67703",
        "source_type": source_type,
        "source_id": f"context:{source_type}",
    }
    payload["requirements"][0]["jira_sources"].append(legacy_source)
    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"].append(
        legacy_source
    )

    plan = parse_frozen_legacy_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot_hash,
        issue_type=None,
        requirements_snapshot=snapshot,
    )

    assert plan.requirements[0].jira_sources[-1].source_type == source_type

    with pytest.raises(PlanSpecError, match="unsupported source type"):
        parse_plan_spec(json.dumps(payload))


def test_generic_custom_field_cannot_be_active_plan_evidence() -> None:
    snapshot = requirements_snapshot()
    generic_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="custom_field",
        source_id="field:customfield_context",
        field_id="customfield_context",
        field_name="Context",
        author="product-owner",
        authority="product",
    )
    snapshot.custom_fields = [
        RequirementArtifact(
            artifact_id=generic_source.source_id,
            source_type="custom_field",
            text="Context that is not configured as Acceptance Criteria.",
            source=generic_source,
            planning_eligible=False,
        )
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["requirements"][0]["jira_sources"].append(
        {
            "issue_key": "ICPM-67703",
            "source_type": "custom_field",
            "source_id": generic_source.source_id,
        }
    )

    with pytest.raises(PlanSpecError, match="absent from the current snapshot"):
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )


def test_plan_must_cover_every_current_requirement_and_acceptance_decision() -> None:
    snapshot = requirements_snapshot_with_current_decisions()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"] = [
        {
            "issue_key": "ICPM-67703",
            "source_type": "custom_field",
            "source_id": "field:acceptance-primary",
        }
    ]

    with pytest.raises(PlanSpecError) as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )

    assert "decision:requirement-comment" in str(error.value)
    assert "decision:acceptance-comment" in str(error.value)


def test_one_matching_source_covers_a_decision_with_duplicate_evidence() -> None:
    snapshot = requirements_snapshot()
    assert snapshot.description is not None
    duplicate_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="comment",
        source_id="comment:duplicate-requirement",
        author="product-owner",
        authority="product",
    )
    snapshot.comments = [
        RequirementArtifact(
            artifact_id=duplicate_source.source_id,
            source_type="comment",
            text=snapshot.current_requirements[0].text,
            source=duplicate_source,
        )
    ]
    snapshot.current_requirements[0].sources.append(duplicate_source)
    snapshot = snapshot.with_content_hash()

    plan = parse_plan_spec(
        json.dumps(plan_payload(snapshot_hash=snapshot.content_hash)),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.requirements[0].jira_sources[0].source_type == "description"


def test_current_supporting_evidence_does_not_require_plan_coverage() -> None:
    snapshot = requirements_snapshot()
    evidence_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="comment",
        source_id="comment:evidence",
        author="designer",
        authority="supporting_evidence",
    )
    snapshot.comments = [
        RequirementArtifact(
            artifact_id="comment:evidence",
            source_type="comment",
            text="The mockup places the column after Status.",
            source=evidence_source,
            kind="supporting_evidence",
        )
    ]
    snapshot.current_requirements.append(
        RequirementDecision(
            id="decision:supporting-evidence",
            text="The mockup places the column after Status.",
            kind="supporting_evidence",
            classification="current",
            sources=[evidence_source],
        )
    )
    snapshot = snapshot.with_content_hash()

    plan = parse_plan_spec(
        json.dumps(plan_payload(snapshot_hash=snapshot.content_hash)),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.issue_key == "ICPM-67703"


@pytest.mark.parametrize(
    ("bucket", "classification"),
    [
        ("superseded_requirements", "superseded"),
        ("inferred_behavior", "inferred"),
        ("unresolved_contradictions", "unresolved_contradiction"),
    ],
)
def test_non_current_decision_sources_cannot_be_promoted_to_active_scope(
    bucket: str,
    classification: str,
) -> None:
    snapshot = requirements_snapshot()
    non_current_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="comment",
        source_id=f"comment:{classification}",
        author="product-owner",
        authority="product",
    )
    snapshot.comments = [
        RequirementArtifact(
            artifact_id=non_current_source.source_id,
            source_type="comment",
            text="This decision is not current scope.",
            source=non_current_source,
        )
    ]
    setattr(
        snapshot,
        bucket,
        [
            RequirementDecision(
                id=f"decision:{classification}",
                text="This decision is not current scope.",
                classification=classification,
                sources=[non_current_source],
            )
        ],
    )
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["requirements"][0]["jira_sources"] = [
        {
            "issue_key": "ICPM-67703",
            "source_type": "comment",
            "source_id": non_current_source.source_id,
        }
    ]

    with pytest.raises(PlanSpecError, match="non-current Jira decision sources"):
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )


def test_role_state_matrix_may_cover_only_role_sensitive_requirements() -> None:
    payload = plan_payload_with_two_requirements()
    payload["role_state_matrix"][0]["acceptance_criterion_ids"].append("AC-sub")

    plan = parse_plan_spec(json.dumps(payload))

    assert plan.role_state_matrix[0].requirement_ids == ["R-role-visibility"]


def test_role_state_matrix_may_cover_only_role_sensitive_acceptance_criteria() -> None:
    payload = plan_payload_with_two_requirements()
    payload["role_state_matrix"][0]["requirement_ids"].append("R-sub-visibility")

    plan = parse_plan_spec(json.dumps(payload))

    assert plan.role_state_matrix[0].acceptance_criterion_ids == ["AC-gc"]


def test_role_neutral_plan_may_use_an_empty_role_state_matrix() -> None:
    payload = plan_payload()
    payload["role_state_matrix"] = []

    plan = parse_plan_spec(json.dumps(payload))

    assert plan.role_state_matrix == []


def test_role_state_matrix_still_rejects_unknown_plan_ids() -> None:
    payload = plan_payload()
    payload["role_state_matrix"][0]["acceptance_criterion_ids"] = ["AC-unknown"]

    with pytest.raises(PlanSpecError, match="unknown role/state acceptance criterion"):
        parse_plan_spec(json.dumps(payload))


def test_linked_issue_description_cannot_be_active_plan_evidence() -> None:
    snapshot = requirements_snapshot()
    relation_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="relation",
        source_id="link:78:outward",
        authority="context",
    )
    phase_two_source = RequirementSource(
        issue_identifier="ICPM-68000",
        source_type="description",
        source_id="description",
        author="product-owner",
        authority="product",
    )
    snapshot.linked_issues = [
        RelatedIssue(
            id="68000",
            identifier="ICPM-68000",
            title="Phase 2",
            relation="relates to",
            direction="outward",
            source=relation_source,
            requirements=[
                RequirementArtifact(
                    artifact_id="description",
                    source_type="description",
                    text="Phase 2 role behavior.",
                    source=phase_two_source,
                )
            ],
        )
    ]
    snapshot.current_requirements = [
        RequirementDecision(
            id="decision:phase-two-requirement",
            text="Phase 2 role behavior.",
            classification="current",
            sources=[phase_two_source],
        ),
        RequirementDecision(
            id="decision:phase-two-acceptance",
            text="The Phase 2 behavior is accepted for each role.",
            kind="acceptance_criterion",
            classification="current",
            sources=[phase_two_source],
        ),
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    linked_source = {
        "issue_key": "ICPM-68000",
        "source_type": "description",
        "source_id": "description",
    }
    payload["requirements"][0]["jira_sources"] = [linked_source]
    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"] = [
        linked_source
    ]

    with pytest.raises(PlanSpecError, match="absent from the current snapshot") as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )

    assert "ICPM-68000:description:description" in str(error.value)


@pytest.mark.parametrize(
    ("snapshot_field", "relationship_type", "direction"),
    [
        ("parent", "parent", "parent"),
        ("children", "child", "child"),
        ("linked_issues", "linked_issue", "outward"),
        ("dependencies", "dependency", "outward"),
    ],
)
def test_related_issue_content_cannot_become_active_plan_evidence(
    snapshot_field: str,
    relationship_type: str,
    direction: str,
) -> None:
    snapshot = requirements_snapshot()
    related_key = f"ICPM-{relationship_type.upper()}"
    relation_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="relation",
        source_id=f"relation:{relationship_type}",
        authority="context",
    )
    comment_source = RequirementSource(
        issue_identifier=related_key,
        source_type="comment",
        source_id=f"comment:{relationship_type}",
        author="product-owner",
        authority="product",
    )
    related = RelatedIssue(
        identifier=related_key,
        relation=relationship_type,
        direction=direction,
        source=relation_source,
        comments=[
            IssueComment(
                id=f"comment:{relationship_type}",
                body="Use the existing column placement.",
                source=comment_source,
            )
        ],
    )
    if snapshot_field == "parent":
        snapshot.parent = related
    else:
        getattr(snapshot, snapshot_field).append(related)
    snapshot = snapshot.with_content_hash()

    anchor_source = {
        "issue_key": "ICPM-67703",
        "source_type": "description",
        "source_id": "description",
    }
    related_comment_source = {
        "issue_key": related_key,
        "source_type": "comment",
        "source_id": comment_source.source_id,
    }
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["requirements"][0]["jira_sources"] = [
        anchor_source,
        related_comment_source,
    ]
    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"] = [
        anchor_source,
        related_comment_source,
    ]

    with pytest.raises(PlanSpecError, match="absent from the current snapshot") as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )

    assert f"{related_key}:comment:{comment_source.source_id}" in str(error.value)


def test_complete_attachments_do_not_require_active_citations() -> None:
    snapshot = requirements_snapshot()
    root_attachment = complete_attachment(
        "ICPM-67703",
        "root-mockup",
        "Column placement evidence.",
    )
    related_attachment = complete_attachment(
        "ICPM-68000",
        "phase-two-mockup",
        "Phase 2 placement evidence.",
    )
    snapshot.attachments = [root_attachment]
    snapshot.linked_issues = [
        RelatedIssue(
            identifier="ICPM-68000",
            relation="relates to",
            direction="outward",
            source=RequirementSource(
                issue_identifier="ICPM-67703",
                source_type="relation",
                source_id="link:phase-two",
                authority="context",
            ),
            attachments=[related_attachment],
        )
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.issue_key == "ICPM-67703"


def test_canonical_role_is_authoritative_over_human_readable_label() -> None:
    payload = plan_payload()
    payload["role_state_matrix"][0]["role"] = (
        "All users: GC / Sub / GC acting-as-Sub"
    )
    payload["role_state_matrix"][0]["canonical_role"] = "all"

    plan = parse_plan_spec(json.dumps(payload))

    assert plan.role_state_matrix[0].canonical_role == "all"

def test_distinct_role_rows_are_structurally_valid() -> None:
    snapshot = requirements_snapshot()
    assert snapshot.description is not None
    snapshot.current_requirements = [
        RequirementDecision(
            id="decision:contractor-roles",
            text="General contractor and subcontractor users have distinct behavior.",
            classification="current",
            sources=[snapshot.description.source],
        ),
        RequirementDecision(
            id="decision:contractor-roles-acceptance",
            text="The role behavior is observable.",
            kind="acceptance_criterion",
            classification="current",
            sources=[snapshot.description.source],
        ),
    ]
    role_mockup = complete_attachment(
        "ICPM-67703",
        "gc-as-sub",
        "GC acting-as-Sub follows the Sub column placement.",
    )
    snapshot.attachments = [role_mockup]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["role_state_matrix"] = [
        {
            "canonical_role": "gc",
            "role": "General Contractor",
            "state": "default",
            "expected_behavior": "Show GC behavior.",
            "requirement_ids": ["R-role-visibility"],
            "acceptance_criterion_ids": ["AC-gc"],
        },
        {
            "canonical_role": "sub",
            "role": "Subcontractor",
            "state": "default",
            "expected_behavior": "Show Sub behavior.",
            "requirement_ids": ["R-role-visibility"],
            "acceptance_criterion_ids": ["AC-gc"],
        },
        {
            "canonical_role": "gc_as_sub",
            "role": "General contractor acting as a subcontractor",
            "state": "acting role",
            "expected_behavior": "Show GC-as-Sub behavior.",
            "requirement_ids": ["R-role-visibility"],
            "acceptance_criterion_ids": ["AC-gc"],
        },
    ]

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert len(plan.role_state_matrix) == 3


def test_role_neutral_technical_requirement_allows_generic_matrix_row() -> None:
    snapshot = requirements_snapshot()
    assert snapshot.description is not None
    snapshot.current_requirements = [
        RequirementDecision(
            id="decision:cache",
            text="Tune cache invalidation for worker nodes.",
            classification="current",
            sources=[snapshot.description.source],
        ),
        RequirementDecision(
            id="decision:cache-acceptance",
            text="Cache invalidation is observable.",
            kind="acceptance_criterion",
            classification="current",
            sources=[snapshot.description.source],
        ),
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["role_state_matrix"][0]["role"] = "All users"
    payload["role_state_matrix"][0]["canonical_role"] = "all"

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[0].role == "All users"


def test_attachment_role_summary_does_not_influence_matrix() -> None:
    snapshot = requirements_snapshot()
    neutral_mockup = complete_attachment(
        "ICPM-67703",
        "neutral-roles",
        "GC: not shown; Sub: not visible; GC acting as Sub: absent",
    )
    snapshot.attachments = [neutral_mockup]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["role_state_matrix"][0]["role"] = "All users"
    payload["role_state_matrix"][0]["canonical_role"] = "all"

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[0].role == "All users"


def test_role_wording_does_not_manufacture_a_required_matrix_row() -> None:
    snapshot = requirements_snapshot()
    assert snapshot.description is not None
    snapshot.current_requirements = [
        RequirementDecision(
            id="decision:sub-not-applicable",
            text="Sub is not applicable for archived projects.",
            classification="current",
            sources=[snapshot.description.source],
        ),
        RequirementDecision(
            id="decision:sub-not-applicable-acceptance",
            text="The archived-project behavior is observable.",
            kind="acceptance_criterion",
            classification="current",
            sources=[snapshot.description.source],
        ),
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["role_state_matrix"][0]["role"] = "All users"
    payload["role_state_matrix"][0]["canonical_role"] = "all"

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[0].canonical_role == "all"


def test_current_decision_coverage_distinguishes_colliding_source_types() -> None:
    snapshot = requirements_snapshot()
    comment_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="comment",
        source_id="shared-id",
        author="product-owner",
        authority="product",
    )
    snapshot.comments = [
        RequirementArtifact(
            artifact_id="shared-id",
            source_type="comment",
            text="Apply the current placement decision.",
            source=comment_source,
        )
    ]
    custom_field_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="custom_field",
        source_id="shared-id",
        field_id="customfield_context",
        field_name="Context",
        author="product-owner",
        authority="product",
    )
    snapshot.custom_fields = [
        RequirementArtifact(
            artifact_id="shared-id",
            source_type="custom_field",
            text="Placement evidence without a current decision.",
            source=custom_field_source,
            kind="acceptance_criterion",
        )
    ]
    snapshot.current_requirements = [
        RequirementDecision(
            id="decision:comment-collision",
            text="Apply the current placement decision.",
            classification="current",
            sources=[comment_source],
        )
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["requirements"][0]["jira_sources"] = [
        {
            "issue_key": "ICPM-67703",
            "source_type": "custom_field",
            "source_id": "shared-id",
        }
    ]

    with pytest.raises(PlanSpecError) as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )

    assert "decision:comment-collision" in str(error.value)
    assert "ICPM-67703:comment:shared-id" in str(error.value)


@pytest.mark.parametrize(
    "source_issue",
    ["ICPM-67703", "ICPM-68000"],
)
def test_attachment_unit_cannot_anchor_plan_requirement(
    source_issue: str,
) -> None:
    snapshot = requirements_snapshot()
    attachment = complete_attachment(
        source_issue,
        "column-placement",
        "The screenshot places Cost immediately after Budget.",
    )
    unit_source = attachment.source.model_copy(
        update={
            "source_id": f"{attachment.source.source_id}#unit:placement",
            "location": "decision-unit:placement",
        }
    )
    if source_issue == snapshot.issue_identifier:
        snapshot.attachments = [attachment]
    else:
        snapshot.linked_issues = [
            RelatedIssue(
                identifier=source_issue,
                relation="relates to",
                direction="outward",
                source=RequirementSource(
                    issue_identifier=snapshot.issue_identifier,
                    source_type="relation",
                    source_id=f"link:{source_issue}",
                    authority="context",
                ),
                attachments=[attachment],
            )
        ]
    acceptance_decision = next(
        decision
        for decision in snapshot.current_requirements
        if decision.kind == "acceptance_criterion"
    )
    snapshot.current_requirements = [
        RequirementDecision(
            id=f"jira:{source_issue}:{unit_source.source_id}",
            text="The screenshot places Cost immediately after Budget.",
            kind="supporting_evidence",
            classification="current",
            sources=[unit_source],
        ),
        acceptance_decision,
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["requirements"][0]["jira_sources"] = [
        {
            "issue_key": source_issue,
            "source_type": "attachment",
            "source_id": unit_source.source_id,
            "location": unit_source.location,
        }
    ]

    with pytest.raises(PlanSpecError, match="description.*custom_field.*comment"):
        parse_plan_spec(json.dumps(payload))


def test_attachment_cannot_be_active_acceptance_evidence() -> None:
    snapshot = RequirementsSnapshot(
        issue_id="67703",
        issue_identifier="ICPM-67703",
        issue_url="https://jira.example.test/browse/ICPM-67703",
    )
    attachment = complete_attachment(
        "ICPM-67703",
        "acceptance-label-only",
        "Acceptance evidence: the Cost column is visible.",
    )
    snapshot.attachments = [attachment]
    snapshot.current_requirements = [
        RequirementDecision(
            id="jira:ICPM-67703:attachment:acceptance-label-only",
            text=attachment.analysis.summary,
            kind="supporting_evidence",
            classification="current",
            sources=[attachment.source],
        )
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    attachment_source = {
        "issue_key": "ICPM-67703",
        "source_type": "attachment",
        "source_id": attachment.source.source_id,
    }
    payload["requirements"][0]["jira_sources"] = [attachment_source]
    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"] = [
        attachment_source
    ]

    with pytest.raises(PlanSpecError, match="description.*custom_field.*comment"):
        parse_plan_spec(json.dumps(payload))


@pytest.mark.parametrize(
    ("target", "unanchored_id"),
    [
        ("requirement", "R-sub-visibility"),
        ("acceptance_criterion", "AC-sub"),
    ],
)
def test_each_active_scope_item_requires_a_current_decision_source(
    target: str,
    unanchored_id: str,
) -> None:
    snapshot = requirements_snapshot()
    auxiliary_source_model = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="comment",
        source_id=f"comment:auxiliary-{target}",
        author="designer",
        authority="supporting_evidence",
    )
    snapshot.comments = [
        RequirementArtifact(
            artifact_id=auxiliary_source_model.source_id,
            source_type="comment",
            text="Supporting placement evidence.",
            source=auxiliary_source_model,
            kind="supporting_evidence",
        )
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload_with_two_requirements()
    payload["requirements_snapshot_hash"] = snapshot.content_hash
    payload["role_state_matrix"][0]["requirement_ids"].append(
        "R-sub-visibility"
    )
    payload["role_state_matrix"][0]["acceptance_criterion_ids"].append("AC-sub")
    auxiliary_source = {
        "issue_key": "ICPM-67703",
        "source_type": "comment",
        "source_id": auxiliary_source_model.source_id,
    }
    if target == "requirement":
        payload["requirements"][1]["jira_sources"] = [auxiliary_source]
    else:
        payload["requirements"][1]["acceptance_criteria"][0]["jira_sources"] = [
            auxiliary_source
        ]

    with pytest.raises(PlanSpecError, match="current authoritative Jira decision source") as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )

    assert unanchored_id in str(error.value)


def test_each_active_scope_item_requires_a_matching_decision_kind() -> None:
    snapshot = requirements_snapshot_with_current_decisions()
    payload = plan_payload_with_two_requirements()
    payload["requirements_snapshot_hash"] = snapshot.content_hash
    payload["role_state_matrix"][0]["requirement_ids"].append(
        "R-sub-visibility"
    )
    payload["role_state_matrix"][0]["acceptance_criterion_ids"].append("AC-sub")

    requirement_sources = [
        {
            "issue_key": "ICPM-67703",
            "source_type": "description",
            "source_id": "description",
        },
        {
            "issue_key": "ICPM-67703",
            "source_type": "comment",
            "source_id": "comment:requirement-current",
        },
    ]
    acceptance_sources = [
        {
            "issue_key": "ICPM-67703",
            "source_type": "custom_field",
            "source_id": "field:acceptance-primary",
        },
        {
            "issue_key": "ICPM-67703",
            "source_type": "comment",
            "source_id": "comment:acceptance-current",
        },
    ]
    payload["requirements"][0]["jira_sources"] = requirement_sources
    payload["requirements"][1]["jira_sources"] = [acceptance_sources[0]]
    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"] = (
        acceptance_sources
    )
    payload["requirements"][1]["acceptance_criteria"][0]["jira_sources"] = [
        requirement_sources[0]
    ]

    with pytest.raises(PlanSpecError, match="matching-layer current") as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )

    assert "requirements R-sub-visibility" in str(error.value)
    assert "acceptance criteria AC-sub" in str(error.value)


def test_v4_requires_exact_current_unit_citation() -> None:
    snapshot = requirements_snapshot()
    base_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="custom_field",
        source_id="field:customfield_acceptance",
        field_id="customfield_acceptance",
        field_name="Acceptance Criteria",
        author="product-owner",
        authority="product",
    )
    unit_source = base_source.model_copy(
        update={"source_id": "field:customfield_acceptance#unit:ac01"}
    )
    snapshot.custom_fields = [
        RequirementArtifact(
            artifact_id=base_source.source_id,
            source_type="custom_field",
            text="The role-aware behavior is visible.",
            source=base_source,
            kind="acceptance_criterion",
        )
    ]
    snapshot.current_requirements.append(
        RequirementDecision(
            id="decision:acceptance-unit",
            text="The role-aware behavior is visible.",
            kind="acceptance_criterion",
            classification="current",
            sources=[unit_source],
        )
    )
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"].append(
        {
            "issue_key": "ICPM-67703",
            "source_type": "custom_field",
            "source_id": base_source.source_id,
        }
    )
    with pytest.raises(PlanSpecError, match="does not cover every current") as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            requirements_snapshot=snapshot,
        )

    assert unit_source.source_id in str(error.value)

    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"][-1][
        "source_id"
    ] = unit_source.source_id
    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )
    assert plan.requirements[0].acceptance_criteria[0].jira_sources[-1].source_id == (
        unit_source.source_id
    )


@pytest.mark.parametrize(
    "schema_version",
    [
        "jira-requirements/v1",
        "jira-requirements/v2",
        "jira-requirements/v3",
    ],
)
def test_frozen_pre_v4_plan_retains_base_artifact_unit_coverage(
    schema_version: str,
) -> None:
    snapshot = requirements_snapshot().model_copy(
        update={"schema_version": schema_version}
    )
    base_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="custom_field",
        source_id="field:customfield_acceptance",
        field_id="customfield_acceptance",
        field_name="Acceptance Criteria",
        author="product-owner",
        authority="product",
    )
    unit_source = base_source.model_copy(
        update={"source_id": "field:customfield_acceptance#unit:ac01"}
    )
    snapshot.custom_fields = [
        RequirementArtifact(
            artifact_id=base_source.source_id,
            source_type="custom_field",
            text="The role-aware behavior is visible.",
            source=base_source,
            kind="acceptance_criterion",
        )
    ]
    snapshot.current_requirements.append(
        RequirementDecision(
            id="decision:acceptance-unit",
            text="The role-aware behavior is visible.",
            kind="acceptance_criterion",
            classification="current",
            sources=[unit_source],
        )
    )
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["requirements"][0]["acceptance_criteria"][0]["jira_sources"].append(
        {
            "issue_key": "ICPM-67703",
            "source_type": "custom_field",
            "source_id": base_source.source_id,
        }
    )
    payload["affected_surface"]["repositories"] = ["./foyr2"]
    payload["baseline_repository_shas"][0]["repository"] = "./foyr2"
    payload["affected_surface"]["files"][0]["repository"] = "./foyr2"
    payload["existing_precedents"][0]["repository"] = "./foyr2"

    plan = parse_frozen_legacy_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        issue_type=None,
        requirements_snapshot=snapshot,
    )

    assert plan.requirements[0].acceptance_criteria[0].jira_sources[-1].source_id == (
        base_source.source_id
    )
    assert plan.affected_surface.repositories == ["./foyr2"]


def test_jira_role_words_do_not_impose_derived_matrix_rows() -> None:
    snapshot = requirements_snapshot()
    assert snapshot.description is not None
    snapshot.current_requirements[0].text = (
        "GCs, Subs, and GCs acting as Subs have distinct behavior."
    )
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["role_state_matrix"][0].update(
        {"canonical_role": "all", "role": "All users"}
    )

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[0].canonical_role == "all"


@pytest.mark.parametrize("role", ["Non-GC users", "All except GC"])
def test_human_readable_role_label_is_not_lexically_validated(role: str) -> None:
    payload = plan_payload()
    payload["role_state_matrix"][0]["role"] = role

    plan = parse_plan_spec(json.dumps(payload))

    assert plan.role_state_matrix[0].canonical_role == "gc"


def test_gc_acting_as_subcontractor_wording_does_not_block_plan() -> None:
    snapshot = requirements_snapshot()
    snapshot.current_requirements[1].text = (
        "Given a General Contractor is acting as a Subcontractor on a project, "
        "the Project Created Date cell is blank."
    )
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["role_state_matrix"][0].update(
        {
            "canonical_role": "gc_as_sub",
            "role": "General Contractor is acting as a Subcontractor",
            "state": "project role",
            "expected_behavior": "Leave Project Created Date blank.",
        }
    )

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[0].canonical_role == "gc_as_sub"


def test_grouped_attachment_role_words_do_not_require_rows() -> None:
    snapshot = requirements_snapshot()
    attachment = complete_attachment(
        "ICPM-67703",
        "grouped-absent-roles",
        "GC and Sub are not shown; GC acting as Sub is absent.",
    )
    snapshot.attachments = [attachment]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["role_state_matrix"][0].update(
        {"canonical_role": "all", "role": "All users"}
    )

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[0].canonical_role == "all"


def test_role_matrix_ids_are_not_bound_by_lexical_source_roles() -> None:
    snapshot = requirements_snapshot()
    assert snapshot.description is not None
    sub_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="comment",
        source_id="comment:sub-requirement",
        author="product-owner",
        authority="product",
    )
    snapshot.comments = [
        RequirementArtifact(
            artifact_id=sub_source.source_id,
            source_type="comment",
            text="A Sub receives the subcontractor behavior.",
            source=sub_source,
        )
    ]
    snapshot.current_requirements = [
        RequirementDecision(
            id="decision:gc-source",
            text="A GC receives the general-contractor behavior.",
            classification="current",
            sources=[snapshot.description.source],
        ),
        RequirementDecision(
            id="decision:sub-source",
            text="A Sub receives the subcontractor behavior.",
            classification="current",
            sources=[sub_source],
        ),
        RequirementDecision(
            id="decision:gc-source-acceptance",
            text="The first role behavior is observable.",
            kind="acceptance_criterion",
            classification="current",
            sources=[snapshot.description.source],
        ),
        RequirementDecision(
            id="decision:sub-source-acceptance",
            text="The second role behavior is observable.",
            kind="acceptance_criterion",
            classification="current",
            sources=[sub_source],
        ),
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload_with_two_requirements()
    payload["requirements_snapshot_hash"] = snapshot.content_hash
    sub_jira_source = {
        "issue_key": "ICPM-67703",
        "source_type": "comment",
        "source_id": sub_source.source_id,
    }
    payload["requirements"][1]["jira_sources"] = [sub_jira_source]
    payload["requirements"][1]["acceptance_criteria"][0]["jira_sources"] = [
        sub_jira_source
    ]
    payload["role_state_matrix"] = [
        {
            "canonical_role": "gc",
            "role": "GC",
            "state": "default",
            "expected_behavior": "Show the GC behavior.",
            "requirement_ids": ["R-sub-visibility"],
            "acceptance_criterion_ids": ["AC-gc"],
        },
        {
            "canonical_role": "sub",
            "role": "Sub",
            "state": "default",
            "expected_behavior": "Show the Sub behavior.",
            "requirement_ids": ["R-role-visibility"],
            "acceptance_criterion_ids": ["AC-sub"],
        },
    ]

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[0].requirement_ids == ["R-sub-visibility"]


def test_uncited_attachment_role_words_do_not_bind_matrix_ids() -> None:
    snapshot = requirements_snapshot()
    gc_mockup = complete_attachment(
        "ICPM-67703",
        "gc-acceptance",
        "GC screenshot shows the role-specific column.",
    )
    snapshot.attachments = [gc_mockup]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload_with_two_requirements()
    payload["requirements_snapshot_hash"] = snapshot.content_hash
    payload["role_state_matrix"] = [
        {
            "canonical_role": "gc",
            "role": "GC",
            "state": "default",
            "expected_behavior": "Show the GC behavior.",
            "requirement_ids": ["R-role-visibility"],
            "acceptance_criterion_ids": ["AC-gc"],
        },
        {
            "canonical_role": "sub",
            "role": "Sub",
            "state": "default",
            "expected_behavior": "Show the Sub behavior.",
            "requirement_ids": ["R-sub-visibility"],
            "acceptance_criterion_ids": ["AC-sub"],
        },
    ]

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[1].acceptance_criterion_ids == ["AC-sub"]


@pytest.mark.parametrize(
    ("decision_kind", "role_requirement_ids", "role_acceptance_ids"),
    [
        ("requirement", ["R-role-visibility"], []),
        ("acceptance_criterion", [], ["AC-gc"]),
    ],
)
def test_role_binding_preserves_requirement_and_acceptance_layers(
    decision_kind: str,
    role_requirement_ids: list[str],
    role_acceptance_ids: list[str],
) -> None:
    snapshot = requirements_snapshot()
    assert snapshot.description is not None
    snapshot.current_requirements = [
        RequirementDecision(
            id=f"decision:sub-{decision_kind}",
            text="Sub behavior applies in the active state.",
            kind=decision_kind,
            classification="current",
            sources=[snapshot.description.source],
        ),
        RequirementDecision(
            id=f"decision:neutral-{'acceptance' if decision_kind == 'requirement' else 'requirement'}",
            text="The behavior has a matching-layer anchor.",
            kind=(
                "acceptance_criterion"
                if decision_kind == "requirement"
                else "requirement"
            ),
            classification="current",
            sources=[snapshot.description.source],
        ),
    ]
    snapshot = snapshot.with_content_hash()
    payload = plan_payload(snapshot_hash=snapshot.content_hash)
    payload["role_state_matrix"] = [
        {
            "canonical_role": "sub",
            "role": "Sub",
            "state": "active",
            "expected_behavior": "Apply the Sub behavior.",
            "requirement_ids": role_requirement_ids,
            "acceptance_criterion_ids": role_acceptance_ids,
        },
        {
            "canonical_role": "all",
            "role": "All users",
            "state": "active",
            "expected_behavior": "Cover the other traceability layer.",
            "requirement_ids": [] if role_requirement_ids else ["R-role-visibility"],
            "acceptance_criterion_ids": [] if role_acceptance_ids else ["AC-gc"],
        },
    ]

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )

    assert plan.role_state_matrix[0].role == "Sub"


def test_epic_requires_decomposition_or_explicit_single_change_approval() -> None:
    payload = plan_payload()

    with pytest.raises(PlanSpecError, match="Epic PlanSpec"):
        parse_plan_spec(json.dumps(payload), issue_type="Epic")

    payload["epic_strategy"] = {
        "mode": "single_change",
        "rationale": "The change is bounded to one independently releasable surface.",
        "bounded_child_plans": [],
        "requires_explicit_single_change_approval": True,
    }
    plan = parse_plan_spec(json.dumps(payload), issue_type="Epic")

    assert plan.epic_strategy is not None
    assert plan.epic_strategy.requires_explicit_single_change_approval


@pytest.mark.parametrize(
    "issue_key",
    [None, "", "phase-two", "ICPM-NOT-A-NUMBER"],
)
def test_decomposed_epic_requires_a_real_child_jira_issue_key(
    issue_key: str | None,
) -> None:
    payload = decomposed_epic_payload()
    if issue_key is None:
        del payload["epic_strategy"]["bounded_child_plans"][0]["issue_key"]
    else:
        payload["epic_strategy"]["bounded_child_plans"][0]["issue_key"] = issue_key

    with pytest.raises(PlanSpecError, match="issue_key"):
        parse_plan_spec(json.dumps(payload), issue_type="Epic")


def test_decomposed_epic_child_plan_ids_must_be_unique() -> None:
    payload = decomposed_epic_payload()
    payload["epic_strategy"]["bounded_child_plans"][1]["id"] = "child-plan-one"

    with pytest.raises(PlanSpecError, match="duplicate child-plan ID"):
        parse_plan_spec(json.dumps(payload), issue_type="Epic")


def test_decomposed_epic_child_issue_keys_must_be_unique() -> None:
    payload = decomposed_epic_payload()
    payload["epic_strategy"]["bounded_child_plans"][1]["issue_key"] = "ICPM-68001"

    with pytest.raises(PlanSpecError, match="duplicate child-plan Jira issue key"):
        parse_plan_spec(json.dumps(payload), issue_type="Epic")


def test_decomposed_epic_keeps_acceptance_with_owning_requirement() -> None:
    payload = decomposed_epic_payload()
    children = payload["epic_strategy"]["bounded_child_plans"]
    children[0]["acceptance_criterion_ids"] = ["AC-sub"]
    children[1]["acceptance_criterion_ids"] = ["AC-gc"]

    with pytest.raises(PlanSpecError, match="owning requirements") as error:
        parse_plan_spec(json.dumps(payload), issue_type="Epic")

    assert "AC-gc" in str(error.value)
    assert "AC-sub" in str(error.value)


def test_precedent_repository_and_path_must_be_statically_bounded() -> None:
    payload = plan_payload()
    payload["existing_precedents"][0]["repository"] = "missing-repo"

    with pytest.raises(PlanSpecError, match="undeclared repository"):
        parse_plan_spec(json.dumps(payload))

    payload = plan_payload()
    payload["existing_precedents"][0]["path"] = "../outside.py"
    with pytest.raises(PlanSpecError, match="must stay within"):
        parse_plan_spec(json.dumps(payload))


def test_repository_paths_are_normalized_across_the_current_plan() -> None:
    payload = plan_payload()
    payload["affected_surface"]["repositories"] = ["./services/./api/"]
    payload["baseline_repository_shas"][0]["repository"] = "services/api"
    payload["affected_surface"]["files"][0]["repository"] = "services//api"
    payload["existing_precedents"][0]["repository"] = "services\\api"

    plan = parse_plan_spec(json.dumps(payload))

    assert plan.affected_surface.repositories == ["services/api"]
    assert plan.baseline_repository_shas[0].repository == "services/api"
    assert plan.affected_surface.files[0].repository == "services/api"
    assert plan.existing_precedents[0].repository == "services/api"


@pytest.mark.parametrize(
    "aliases",
    [
        ["services/api", "services/./api"],
        [".", "./"],
    ],
)
def test_current_plan_rejects_duplicate_repository_aliases(
    aliases: list[str],
) -> None:
    payload = plan_payload()
    payload["affected_surface"]["repositories"] = aliases
    payload["baseline_repository_shas"] = [
        {
            "repository": repository,
            "sha": "1234567890abcdef1234567890abcdef12345678",
        }
        for repository in aliases
    ]
    payload["affected_surface"]["files"][0]["repository"] = aliases[0]
    payload["existing_precedents"][0]["repository"] = aliases[0]

    with pytest.raises(PlanSpecError, match="aliases.*same workspace-relative"):
        parse_plan_spec(json.dumps(payload))


def test_precedent_path_runtime_validation_checks_existence_and_symlinks(
    tmp_path,
) -> None:
    repository = tmp_path / "foyr2"
    precedent = repository / "foyr/client_src/js/cpm/home/home.js"
    precedent.parent.mkdir(parents=True)
    precedent.write_text("// existing precedent", encoding="utf-8")
    plan = parse_plan_spec(json.dumps(plan_payload()))

    assert validate_plan_precedent_paths(plan, tmp_path) is None

    missing_payload = plan_payload()
    missing_payload["existing_precedents"][0]["path"] = "missing.py"
    missing_plan = parse_plan_spec(json.dumps(missing_payload))
    assert "does not exist" in (
        validate_plan_precedent_paths(missing_plan, tmp_path) or ""
    )

    outside = tmp_path / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    escaped = repository / "escaped.py"
    escaped.symlink_to(outside)
    escaped_payload = plan_payload()
    escaped_payload["existing_precedents"][0]["path"] = "escaped.py"
    escaped_plan = parse_plan_spec(json.dumps(escaped_payload))
    assert "resolves outside" in (
        validate_plan_precedent_paths(escaped_plan, tmp_path) or ""
    )


def test_decomposed_epic_child_plans_must_reference_snapshot_children_or_links() -> None:
    snapshot = requirements_snapshot()
    snapshot.children = [
        RelatedIssue(
            identifier="ICPM-68001",
            relation="Epic child",
            direction="child",
            source=RequirementSource(
                issue_identifier="ICPM-67703",
                source_type="relation",
                source_id="child:ICPM-68001",
                authority="context",
            ),
        )
    ]
    snapshot.linked_issues = [
        RelatedIssue(
            identifier="ICPM-68002",
            relation="relates to",
            direction="outward",
            source=RequirementSource(
                issue_identifier="ICPM-67703",
                source_type="relation",
                source_id="link:ICPM-68002",
                authority="context",
            ),
        )
    ]
    snapshot = snapshot.with_content_hash()
    payload = decomposed_epic_payload(snapshot_hash=snapshot.content_hash)

    plan = parse_plan_spec(
        json.dumps(payload),
        expected_issue_key="ICPM-67703",
        expected_snapshot_hash=snapshot.content_hash,
        issue_type="Epic",
        requirements_snapshot=snapshot,
    )

    assert plan.epic_strategy is not None
    assert [
        child.issue_key for child in plan.epic_strategy.bounded_child_plans
    ] == ["ICPM-68001", "ICPM-68002"]

    payload["epic_strategy"]["bounded_child_plans"][1]["issue_key"] = "ICPM-69999"
    with pytest.raises(PlanSpecError, match="child or linked issues") as error:
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            issue_type="Epic",
            requirements_snapshot=snapshot,
        )

    assert "ICPM-69999" in str(error.value)


def test_dependency_only_issue_is_not_an_epic_child_plan_target() -> None:
    snapshot = requirements_snapshot()
    snapshot.dependencies = [
        RelatedIssue(
            identifier="ICPM-68001",
            relation="blocks",
            direction="outward",
            is_dependency=True,
            source=RequirementSource(
                issue_identifier="ICPM-67703",
                source_type="relation",
                source_id="dependency:ICPM-68001",
                authority="context",
            ),
        )
    ]
    snapshot.linked_issues = [
        RelatedIssue(
            identifier="ICPM-68002",
            relation="relates to",
            direction="outward",
            source=RequirementSource(
                issue_identifier="ICPM-67703",
                source_type="relation",
                source_id="link:ICPM-68002",
                authority="context",
            ),
        )
    ]
    snapshot = snapshot.with_content_hash()
    payload = decomposed_epic_payload(snapshot_hash=snapshot.content_hash)

    with pytest.raises(PlanSpecError, match="ICPM-68001"):
        parse_plan_spec(
            json.dumps(payload),
            expected_issue_key="ICPM-67703",
            expected_snapshot_hash=snapshot.content_hash,
            issue_type="Epic",
            requirements_snapshot=snapshot,
        )


def requirements_snapshot() -> RequirementsSnapshot:
    source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="description",
        source_id="description",
        author="product-owner",
        authority="product",
    )
    return RequirementsSnapshot(
        issue_id="67703",
        issue_identifier="ICPM-67703",
        issue_url="https://jira.example.test/browse/ICPM-67703",
        description=RequirementArtifact(
            artifact_id="description",
            source_type="description",
            text="Add the role-aware behavior.",
            source=source,
        ),
        current_requirements=[
            RequirementDecision(
                id="decision:description",
                text="Add the role-aware behavior.",
                classification="current",
                sources=[source],
            ),
            RequirementDecision(
                id="decision:description-acceptance",
                text="The role-aware behavior is observable.",
                kind="acceptance_criterion",
                classification="current",
                sources=[source],
            ),
        ],
    )

def complete_attachment(
    issue_key: str,
    attachment_id: str,
    summary: str,
) -> IssueAttachment:
    return IssueAttachment(
        id=attachment_id,
        filename=f"{attachment_id}.png",
        mime_type="image/png",
        source=RequirementSource(
            issue_identifier=issue_key,
            source_type="attachment",
            source_id=f"attachment:{attachment_id}",
            author="designer",
            authority="supporting_evidence",
        ),
        analysis=AttachmentAnalysis(
            status="complete",
            modality="vision",
            summary=summary,
        ),
    )



def requirements_snapshot_with_current_decisions() -> RequirementsSnapshot:
    snapshot = requirements_snapshot()
    assert snapshot.description is not None
    requirement_comment_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="comment",
        source_id="comment:requirement-current",
        author="product-owner",
        authority="product",
    )
    primary_acceptance_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="custom_field",
        source_id="field:acceptance-primary",
        field_id="customfield_acceptance",
        field_name="Acceptance Criteria",
        author="product-owner",
        authority="product",
    )
    acceptance_comment_source = RequirementSource(
        issue_identifier="ICPM-67703",
        source_type="comment",
        source_id="comment:acceptance-current",
        author="product-owner",
        authority="product",
    )
    snapshot.custom_fields = [
        RequirementArtifact(
            artifact_id="field:acceptance-primary",
            source_type="custom_field",
            text="A GC sees the GC behavior.",
            source=primary_acceptance_source,
            kind="acceptance_criterion",
        )
    ]
    snapshot.comments = [
        RequirementArtifact(
            artifact_id="comment:requirement-current",
            source_type="comment",
            text="A Sub sees the Sub behavior.",
            source=requirement_comment_source,
        ),
        RequirementArtifact(
            artifact_id="comment:acceptance-current",
            source_type="comment",
            text="GC-as-Sub follows the Sub placement.",
            source=acceptance_comment_source,
            kind="acceptance_criterion",
        ),
    ]
    snapshot.current_requirements = [
        RequirementDecision(
            id="decision:requirement-description",
            text=snapshot.description.text,
            classification="current",
            sources=[snapshot.description.source],
        ),
        RequirementDecision(
            id="decision:requirement-comment",
            text="A Sub sees the Sub behavior.",
            classification="current",
            sources=[requirement_comment_source],
        ),
        RequirementDecision(
            id="decision:acceptance-field",
            text="A GC sees the GC behavior.",
            kind="acceptance_criterion",
            classification="current",
            sources=[primary_acceptance_source],
        ),
        RequirementDecision(
            id="decision:acceptance-comment",
            text="GC-as-Sub follows the Sub placement.",
            kind="acceptance_criterion",
            classification="current",
            sources=[acceptance_comment_source],
        ),
    ]
    return snapshot.with_content_hash()


def plan_payload(*, snapshot_hash: str = SNAPSHOT_HASH) -> dict:
    source = {
        "issue_key": "ICPM-67703",
        "source_type": "description",
        "source_id": "description",
    }
    return {
        "schema_version": "1.0",
        "decision": "ready_for_approval",
        "issue_key": "ICPM-67703",
        "requirements_snapshot_hash": snapshot_hash,
        "baseline_repository_shas": [
            {"repository": "foyr2", "sha": "1234567890abcdef1234567890abcdef12345678"}
        ],
        "requirements": [
            {
                "id": "R-role-visibility",
                "statement": "Show the column according to the active role.",
                "jira_sources": [source],
                "acceptance_criteria": [
                    {
                        "id": "AC-gc",
                        "statement": "A GC sees the GC behavior.",
                        "jira_sources": [source],
                    }
                ],
            }
        ],
        "role_state_matrix": [
            {
                "canonical_role": "gc",
                "role": "GC",
                "state": "default",
                "expected_behavior": "Show the GC-specific value.",
                "requirement_ids": ["R-role-visibility"],
                "acceptance_criterion_ids": ["AC-gc"],
            }
        ],
        "affected_surface": {
            "repositories": ["foyr2"],
            "files": [
                {
                    "repository": "foyr2",
                    "target": "foyr/client_src/js/cpm/home/home.js",
                    "change": "Apply role-specific column behavior.",
                }
            ],
            "apis": [],
            "schemas": [],
            "migrations": [],
            "translations": [],
        },
        "existing_precedents": [
            {
                "repository": "foyr2",
                "path": "foyr/client_src/js/cpm/home/home.js",
                "description": "Existing role checks establish the smallest implementation pattern.",
            }
        ],
        "simplest_implementation": "Extend the existing role predicate and column definition.",
        "assumptions": [],
        "non_goals": ["Redesign the complete table."],
        "prohibited_scope": ["Do not alter unrelated role permissions."],
        "test_cases": [
            {
                "id": "TC-gc",
                "acceptance_criterion_id": "AC-gc",
                "level": "unit",
                "description": "Render the table as a GC.",
                "expected_result": "The GC-specific value is visible.",
            }
        ],
        "rollout": "Release with the normal frontend deployment.",
        "rollback": "Revert the scoped frontend commit.",
        "compatibility": "No API or persisted-data compatibility change.",
        "risks": [
            {
                "id": "risk-role-resolution",
                "severity": "medium",
                "description": "GC-as-Sub may resolve through a different role path.",
                "mitigation": "Cover every role/state row with its mapped test.",
            }
        ],
        "open_questions": [],
        "epic_strategy": None,
    }


def plan_payload_with_two_requirements() -> dict:
    payload = plan_payload()
    source = {
        "issue_key": "ICPM-67703",
        "source_type": "description",
        "source_id": "description",
    }
    payload["requirements"].append(
        {
            "id": "R-sub-visibility",
            "statement": "Show the column for a Sub.",
            "jira_sources": [source],
            "acceptance_criteria": [
                {
                    "id": "AC-sub",
                    "statement": "A Sub sees the Sub behavior.",
                    "jira_sources": [source],
                }
            ],
        }
    )
    payload["test_cases"].append(
        {
            "id": "TC-sub",
            "acceptance_criterion_id": "AC-sub",
            "level": "unit",
            "description": "Render the table as a Sub.",
            "expected_result": "The Sub-specific value is visible.",
        }
    )
    return payload


def decomposed_epic_payload(*, snapshot_hash: str = SNAPSHOT_HASH) -> dict:
    payload = plan_payload_with_two_requirements()
    payload["requirements_snapshot_hash"] = snapshot_hash
    payload["role_state_matrix"][0]["requirement_ids"].append("R-sub-visibility")
    payload["role_state_matrix"][0]["acceptance_criterion_ids"].append("AC-sub")
    payload["epic_strategy"] = {
        "mode": "decomposed",
        "rationale": "Each Jira child is an independently reviewable change.",
        "bounded_child_plans": [
            {
                "id": "child-plan-one",
                "issue_key": "ICPM-68001",
                "scope": "Implement the GC slice.",
                "non_goals": ["Do not implement the Sub slice."],
                "requirement_ids": ["R-role-visibility"],
                "acceptance_criterion_ids": ["AC-gc"],
            },
            {
                "id": "child-plan-two",
                "issue_key": "ICPM-68002",
                "scope": "Implement the Sub slice.",
                "non_goals": ["Do not implement the GC slice."],
                "requirement_ids": ["R-sub-visibility"],
                "acceptance_criterion_ids": ["AC-sub"],
            },
        ],
        "requires_explicit_single_change_approval": False,
    }
    return payload
