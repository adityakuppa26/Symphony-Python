from __future__ import annotations

from ..config import WorkflowConfig
from ..plan_spec import PlanSpec, PlanSpecError


class DevelopmentHandler:
    """Policy for development planning, implementation, and review."""

    scope_instructions = (
        "This is the development workflow. Inspect Jira requirements alongside "
        "the application code, plan the smallest correct change, and implement "
        "only the approved scope before independent code review and handoff."
    )

    def __init__(self, config: WorkflowConfig) -> None:
        self.repositories = tuple(
            path.as_posix() for path in config.workspace.managed_repositories
        )

    def implementation_instructions(self) -> str:
        return (
            self.scope_instructions
            + "\nEdit only these managed repositories: "
            + (", ".join(self.repositories) or "the repositories declared in the approved PlanSpec")
            + ". Follow the exact approved PlanSpec and make the smallest correct "
            "change using existing repository patterns. Do not add unrelated "
            "refactors, abstractions, helpers, or special cases.\n"
            "Running tests is optional. Report tests as passed, failed, or not run "
            "with evidence and residual risk. Missing tools, unavailable environments, "
            "and test failures alone do not require human input or approval to bypass "
            "verification. Complete the source change and hand off once implementation "
            "and code review are complete. Never claim an unrun check passed."
        )

    def planning_instructions(self) -> str:
        return (
            self.implementation_instructions()
            + "\nPlanning is read-only. Inspect the Jira requirements together with "
            "the target code and nearby tracked precedents at the branch merge base. "
            "Cite the applicable existing implementations in the PlanSpec; propose "
            "the minimal change and explain why any new pattern is necessary. "
            "Plan appropriate tests, but test execution is not an approval or handoff gate."
        )

    def review_instructions(self) -> str:
        return (
            self.implementation_instructions()
            + "\nReview independently against the canonical Jira requirements and "
            "approved PlanSpec as clarified by accumulated human decisions for correctness, regressions, minimal scope, and reuse "
            "of established code patterns. Inspect nearby implementations and the "
            "merge-base diff; request deletion or simplification of unnecessary custom "
            "logic. Missing execution evidence alone is residual risk, not a reason "
            "to withhold approval. Concrete code defects still require correction. "
            "Use plan_changes_required if the approved PlanSpec must change. "
            "Do not flag an explicit human override accepted before approval as a defect merely because Jira differs. "
            "This workflow has one approved PlanSpec."
        )

    def validate_plan(self, plan: PlanSpec) -> None:
        outside = set(plan.affected_surface.repositories) - set(self.repositories)
        if self.repositories and outside:
            raise PlanSpecError(
                "PlanSpec includes repositories outside this workflow: "
                + ", ".join(sorted(outside))
            )
