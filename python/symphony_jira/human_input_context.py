from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from .models import RunRecord
from .store import Store


HISTORY_MARKER = "Chronological input history (oldest first; nothing omitted):\n"


HUMAN_DECISION_POLICY = """Human decision policy (applies to every phase):
- Direct operator clarifications and explicit overrides resolve the specific points
  they address. Honor the latest explicit human decision on a point, even when it
  overrides the original Jira wording. Do not expand it to unrelated behavior.
- A clarification or override accepted before the applicable plan approval is part
  of the approved interpretation. Review must not reopen that settled point as a
  defect solely because the original Jira text differs. When the human explicitly
  preserves existing behavior, do not demand a behavior change on the basis of the
  overridden criterion.
- A newer explicit operator decision supersedes an older conflicting decision.
  Generic approval/retry messages and pasted code-review proposals do not silently
  revoke an explicit override. Distinguish direct decisions from review proposals.
  Replanning or invalidating an approval does not erase earlier human decisions;
  it requires a new approval for the revised plan.
- During planning, incorporate applicable decisions into the PlanSpec. Record the
  decision IDs and the resulting exceptions in assumptions, non-goals, or risks;
  keep original Jira citations for traceability without inventing Jira source IDs
  for human input or falsely attributing an override to Jira.
- Implementation and review follow the exact approved PlanSpec together with its
  accepted human decisions. New feedback after approval that changes approved scope
  requires replanning and new approval before code changes. Invalidated or historical
  approvals do not authorize new edits. Integrity and evidence-completeness checks
  still apply.
- Review can flag new correctness or regression evidence outside an accepted
  exception, but must explain what is new instead of repeating an overridden finding.
- This is dashboard/operator history, not Jira comments. Jira comment exclusion
  remains in force. Keep older decisions visible as history, use their recorded
  issue/plan context, and ask only about a genuinely new unresolved conflict."""


def build_human_input_context(
    store: Store,
    issue_identifier: str,
    *,
    through: datetime,
    run_id: str,
    approval_id: str | None = None,
    current_input: dict[str, Any] | None = None,
    previous_run: RunRecord | None = None,
    current_review: dict[str, Any] | None = None,
) -> str:
    """Freeze the whole issue history; include direct callers' current input once."""
    if previous_run and previous_run.issue_identifier != issue_identifier:
        raise ValueError("human context source belongs to a different issue")
    for current in (current_input, current_review):
        if current and current.get("issue_identifier") not in (None, issue_identifier):
            raise ValueError("human input belongs to a different issue")
    history = store.human_input_history(issue_identifier, through=through)
    if previous_run and previous_run.human_input_context:
        # Direct run_once callers can supply input without creating a dashboard
        # input row. Carry those durable decisions forward from the prior run too.
        _, marker, payload = previous_run.human_input_context.partition(HISTORY_MARKER)
        try:
            previous_history = json.loads(payload) if marker else None
        except ValueError as exc:
            raise ValueError("saved human input history is invalid") from exc
        if not isinstance(previous_history, list) or any(
            not isinstance(entry, dict) or not {"id", "created_at", "kind", "text"} <= entry.keys()
            for entry in previous_history
        ):
            raise ValueError("saved human input history is invalid")
        known_ids = {entry["id"] for entry in history}
        history.extend(entry for entry in previous_history if entry["id"] not in known_ids)
    if current_input and str(current_input.get("response") or "").strip():
        entry_id = "human-input:" + str(current_input.get("id") or f"current-{run_id}")
        source_run_id = previous_run.id if previous_run else current_input.get("run_id")
        represented = any(
            entry["id"] == entry_id
            or (
                not current_input.get("id")
                and entry["kind"] in {"operator_feedback", "plan_approval"}
                and entry.get("source_run_id") == source_run_id
                and entry.get("text") == current_input["response"]
            )
            for entry in history
        )
        if not represented:
            history.append({
                "id": entry_id,
                "kind": "plan_approval" if current_input.get("action") == "plan_approval" else "operator_feedback",
                "source_run_id": source_run_id,
                "phase": previous_run.blocked_phase if previous_run else None,
                "created_at": str(current_input.get("created_at") or through.isoformat()),
                "author": current_input.get("approver_identity") or "operator (identity not recorded)",
                "question": current_input.get("question") or (previous_run.error if previous_run else None),
                "text": current_input["response"],
                "approval_id": current_input.get("approval_id"),
            })
    if current_review:
        entry_id = "human-review:" + str(current_review["id"])
        if not any(entry["id"] == entry_id for entry in history):
            history.append({
                "id": entry_id,
                "kind": "code_review_feedback",
                "source_run_id": current_review.get("source_run_id"),
                "phase": "human_review",
                "created_at": str(current_review.get("created_at") or through.isoformat()),
                "author": current_review.get("reviewer_identity"),
                "source_url": current_review.get("source_url"),
                "text": current_review.get("comments"),
            })
    history.sort(key=lambda entry: (entry["created_at"], entry["id"]))
    return (
        "## Accumulated human input for all phases\n\n"
        f"Issue: {issue_identifier}\n"
        f"Run: {run_id}\n"
        f"Captured through: {through.isoformat()}\n"
        f"Plan approval reference at run start: {approval_id or 'none'}\n\n"
        + HUMAN_DECISION_POLICY
        + "\n\n" + HISTORY_MARKER
        + json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True)
    )
