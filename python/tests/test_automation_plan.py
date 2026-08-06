from __future__ import annotations

import json

import pytest

from symphony_jira.automation_plan import (
    AutomationPlan,
    AutomationPlanError,
    automation_plan_json_schema,
    parse_automation_plan,
)
from symphony_jira.plan_spec import PlanSpec


SNAPSHOT_HASH = "a" * 64
DIFF_HASH = "d" * 64
BASELINE_SHA = "e" * 40


def test_fenced_plan_parses_with_canonical_json_hash_and_schema() -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    message = f"Plan follows.\n```json\n{json.dumps(payload, indent=2)}\n```"

    plan = parse(message, development_plan)
    reordered = AutomationPlan.model_validate(dict(reversed(list(payload.items()))))

    assert plan.content_hash() == reordered.content_hash()
    assert json.loads(plan.canonical_json(indent=2))["schema_version"] == "1.0"
    assert json.loads(automation_plan_json_schema())["additionalProperties"] is False


@pytest.mark.parametrize(
    ("field", "replacement", "expected_error"),
    [
        ("issue_key", "ICPM-99", "issue_key"),
        ("requirements_snapshot_hash", "b" * 64, "requirements_snapshot_hash"),
        ("development_plan_spec_hash", "c" * 64, "development_plan_spec_hash"),
        ("development_workspace_diff_hash", "f" * 64, "development_workspace_diff_hash"),
        ("automation_repository", "other", "automation_repository"),
        ("repository_baseline_sha", "f" * 40, "repository_baseline_sha"),
    ],
)
def test_plan_fields_must_match_exact_caller_bindings(
    field: str,
    replacement: str,
    expected_error: str,
) -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload[field] = replacement

    with pytest.raises(AutomationPlanError, match=expected_error):
        parse(json.dumps(payload), development_plan)


def test_expected_plan_hash_must_bind_to_supplied_development_plan() -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload["development_plan_spec_hash"] = "c" * 64

    with pytest.raises(AutomationPlanError, match="supplied development PlanSpec"):
        parse_automation_plan(
            json.dumps(payload),
            expected_issue_key="ICPM-42",
            expected_requirements_snapshot_hash=SNAPSHOT_HASH,
            expected_development_plan_spec_hash="c" * 64,
            expected_development_diff_hash=DIFF_HASH,
            expected_repository="CPM",
            expected_repository_baseline_sha=BASELINE_SHA,
            development_plan_spec=development_plan,
        )


@pytest.mark.parametrize("path", ["/tmp/test.py", "../test.py", "C:/test.py"])
def test_affected_file_paths_must_be_safe_and_relative(path: str) -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload["affected_file_changes"][0]["path"] = path

    with pytest.raises(AutomationPlanError, match="safe relative POSIX path"):
        parse(json.dumps(payload), development_plan)


@pytest.mark.parametrize("path", [".git/config", ".symphony/result.json"])
def test_affected_file_paths_cannot_target_control_metadata(path: str) -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload["affected_file_changes"][0]["path"] = path

    with pytest.raises(AutomationPlanError, match="metadata or Symphony artifacts"):
        parse(json.dumps(payload), development_plan)


def test_normalized_file_path_aliases_are_rejected_as_duplicates() -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    duplicate = dict(payload["affected_file_changes"][0])
    duplicate["path"] = "tests\\test_feature.py"
    payload["affected_file_changes"].append(duplicate)

    with pytest.raises(AutomationPlanError, match="duplicate affected file path"):
        parse(json.dumps(payload), development_plan)


def test_repository_identity_must_be_canonical_before_exact_binding() -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload["automation_repository"] = "./CPM"

    with pytest.raises(AutomationPlanError, match="canonical relative POSIX identity"):
        parse(json.dumps(payload), development_plan)


def test_scenarios_must_reference_the_development_plan() -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload["mapped_scenarios"][0]["requirement_ids"] = ["REQ-unknown"]

    with pytest.raises(AutomationPlanError, match="unknown.*development requirement"):
        parse(json.dumps(payload), development_plan)

    payload["mapped_scenarios"][0]["requirement_ids"] = []
    plan = parse(json.dumps(payload), development_plan)
    assert plan.mapped_scenarios[0].acceptance_criterion_ids == ["AC-1"]


def test_update_plan_may_cover_only_the_relevant_development_subset() -> None:
    development_plan = development_plan_spec().model_copy(deep=True)
    second_requirement = development_plan.requirements[0].model_copy(
        update={
            "id": "REQ-2",
            "acceptance_criteria": [
                development_plan.requirements[0].acceptance_criteria[0].model_copy(
                    update={"id": "AC-2"}
                )
            ],
        }
    )
    development_plan.requirements.append(second_requirement)
    payload = automation_plan_payload(development_plan)

    plan = parse(json.dumps(payload), development_plan)

    assert plan.mapped_scenarios[0].requirement_ids == ["REQ-1"]
    assert plan.mapped_scenarios[0].acceptance_criterion_ids == ["AC-1"]


def test_internal_scenario_references_and_verification_coverage_are_enforced() -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload["mapped_scenarios"].append(
        {
            "id": "SC-existing",
            "description": "Preserve an existing regression scenario.",
            "requirement_ids": ["REQ-1"],
            "acceptance_criterion_ids": ["AC-1"],
        }
    )
    payload["affected_file_changes"][0]["scenario_ids"] = ["SC-unknown"]

    with pytest.raises(AutomationPlanError, match="unknown affected file.*scenario"):
        parse(json.dumps(payload), development_plan)

    payload["affected_file_changes"][0]["scenario_ids"] = ["SC-change"]
    with pytest.raises(AutomationPlanError, match="verification must cover every mapped scenario"):
        parse(json.dumps(payload), development_plan)


def test_update_decision_and_file_changes_must_agree() -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload["affected_file_changes"] = []

    with pytest.raises(AutomationPlanError, match="update_required must include"):
        parse(json.dumps(payload), development_plan)

    payload["decision"] = "no_update_required"
    payload["mapped_scenarios"] = []
    payload["verification"] = []
    plan = parse(json.dumps(payload), development_plan)
    assert plan.affected_file_changes == []

    payload["affected_file_changes"] = [
        {
            "path": "tests/test_feature.py",
            "change_type": "update",
            "description": "Update coverage.",
            "scenario_ids": ["SC-change"],
        }
    ]
    with pytest.raises(AutomationPlanError, match="no_update_required must keep"):
        parse(json.dumps(payload), development_plan)


def test_needs_human_response_and_blocking_questions_are_explicit() -> None:
    development_plan = development_plan_spec()
    with pytest.raises(AutomationPlanError, match="Which environment"):
        parse(
            json.dumps(
                {
                    "decision": "needs_human",
                    "question": "Which environment should this cover?",
                }
            ),
            development_plan,
        )

    payload = automation_plan_payload(development_plan)
    payload["open_questions"] = [
        {
            "id": "Q-1",
            "question": "Which environment should this cover?",
            "blocks_implementation": True,
        }
    ]
    payload["assumptions"] = [
        {
            "id": "AS-1",
            "statement": "The default environment is acceptable.",
            "evidence": "Existing suite configuration.",
            "needs_human": True,
        }
    ]

    assert parse(json.dumps(payload), development_plan).blocking_question() == (
        "Which environment should this cover?"
    )


def test_models_are_strict_and_forbid_extra_fields() -> None:
    development_plan = development_plan_spec()
    payload = automation_plan_payload(development_plan)
    payload["assumptions"] = [
        {
            "id": "AS-1",
            "statement": "An assumption.",
            "evidence": "Repository evidence.",
            "needs_human": "false",
        }
    ]

    with pytest.raises(AutomationPlanError, match="valid boolean"):
        parse(json.dumps(payload), development_plan)

    payload = automation_plan_payload(development_plan)
    payload["unexpected"] = True
    with pytest.raises(AutomationPlanError, match="Extra inputs are not permitted"):
        parse(json.dumps(payload), development_plan)


def parse(message: str, development_plan: PlanSpec) -> AutomationPlan:
    return parse_automation_plan(
        message,
        expected_issue_key="ICPM-42",
        expected_requirements_snapshot_hash=SNAPSHOT_HASH,
        expected_development_plan_spec_hash=development_plan.content_hash(),
        expected_development_diff_hash=DIFF_HASH,
        expected_repository="CPM",
        expected_repository_baseline_sha=BASELINE_SHA,
        development_plan_spec=development_plan,
    )


def automation_plan_payload(development_plan: PlanSpec) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "decision": "update_required",
        "issue_key": "ICPM-42",
        "requirements_snapshot_hash": SNAPSHOT_HASH,
        "development_plan_spec_hash": development_plan.content_hash(),
        "development_workspace_diff_hash": DIFF_HASH,
        "automation_repository": "CPM",
        "repository_baseline_sha": BASELINE_SHA,
        "rationale": "The development change adds behavior not covered by automation.",
        "mapped_scenarios": [
            {
                "id": "SC-change",
                "description": "Exercise the new behavior.",
                "requirement_ids": ["REQ-1"],
                "acceptance_criterion_ids": ["AC-1"],
            }
        ],
        "affected_file_changes": [
            {
                "path": "tests/test_feature.py",
                "change_type": "update",
                "description": "Add the new regression scenario.",
                "scenario_ids": ["SC-change"],
            }
        ],
        "verification": [
            {
                "id": "VERIFY-1",
                "command": "pytest tests/test_feature.py",
                "expected_result": "The targeted regression suite passes.",
                "scenario_ids": ["SC-change"],
            }
        ],
        "risks": [],
        "assumptions": [],
        "open_questions": [],
    }


def development_plan_spec() -> PlanSpec:
    source = {
        "issue_key": "ICPM-42",
        "source_type": "description",
        "source_id": "description",
    }
    return PlanSpec.model_validate(
        {
            "schema_version": "1.0",
            "decision": "ready_for_approval",
            "issue_key": "ICPM-42",
            "requirements_snapshot_hash": SNAPSHOT_HASH,
            "baseline_repository_shas": [
                {"repository": ".", "sha": "1" * 40}
            ],
            "requirements": [
                {
                    "id": "REQ-1",
                    "statement": "Add the feature.",
                    "jira_sources": [source],
                    "acceptance_criteria": [
                        {
                            "id": "AC-1",
                            "statement": "The feature behaves as requested.",
                            "jira_sources": [source],
                        }
                    ],
                }
            ],
            "role_state_matrix": [],
            "affected_surface": {
                "repositories": ["."],
                "files": [],
                "apis": [],
                "schemas": [],
                "migrations": [],
                "translations": [],
            },
            "existing_precedents": [],
            "simplest_implementation": "Implement only the requested feature.",
            "assumptions": [],
            "non_goals": [],
            "prohibited_scope": [],
            "test_cases": [
                {
                    "id": "TC-1",
                    "acceptance_criterion_id": "AC-1",
                    "level": "unit",
                    "description": "Exercise the feature.",
                    "expected_result": "The requested behavior is present.",
                }
            ],
            "rollout": "Normal deployment.",
            "rollback": "Revert the change.",
            "compatibility": "No compatibility impact.",
            "risks": [],
            "open_questions": [],
            "epic_strategy": None,
        }
    )
