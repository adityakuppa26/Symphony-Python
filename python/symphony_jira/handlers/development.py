from __future__ import annotations

from ..config import WorkflowConfig
from ..plan_spec import PlanSpec, PlanSpecError


class DevelopmentHandler:
    """Policy for development planning, implementation, and review."""

    pattern_policy = """Existing-pattern and minimal-change policy:
- Implement only the approved requirement or bug fix. Treat established codebase
  behavior as the default for details the requirement does not change.
- Before editing, inspect the target implementation and the closest comparable
  working feature or test. Prefer the pattern in the same component/module, then
  the same repository. Follow its naming, control flow, data shapes, validation,
  error handling, authorization, UI components, translations, and test style.
- Extend the existing code path or reuse an existing helper/component first.
  Do not introduce a parallel implementation, generic abstraction, new dependency,
  configuration option, feature flag, fallback, retry, cache, or extra behavior
  unless it is necessary for the approved requirement. Explain that necessity
  against the concrete existing implementation, not hypothetical future reuse.
- Keep the diff focused. Exclude opportunistic refactors, unrelated cleanup,
  renames, formatting churn, speculative edge-case behavior, and extra features.
  Add focused regression coverage for the required behavior using existing tests.
- When no suitable precedent exists, use the smallest local implementation that
  satisfies the requirement. Record the reason; do not invent a framework or ask
  for approval of routine implementation details. Ask only about an unresolved
  choice that materially changes user-visible behavior or approved scope.
- Explicit requirements and demonstrated correctness take precedence over a
  precedent. Preserve accepted operator decisions and approved exceptions.
- Before finishing, inspect every changed file: connect each change to the
  requirement or its necessary regression coverage, and remove unnecessary work.
  Report the concrete precedent followed and any necessary deviation briefly."""

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
            + self.pattern_policy + "\n"
            + "End implementation with a JSON object containing implementation_status "
            "(complete or continue), summary, and remaining_work (a list). Use continue "
            "when approved work remains; Symphony will resume implementation before "
            "independent review. Use complete only when all approved source changes "
            "are implemented; unavailable test execution alone is not unfinished code.\n"
            "If an accepted human decision requires changing the approved PlanSpec, "
            "return JSON with decision plan_changes_required and a specific reason. "
            "Symphony routes that response to planning; do not ask the operator to "
            "route it or repeat a decision already supplied. Preserve accepted "
            "exceptions and verification deferrals in the reason.\n"
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
            "Make reuse the planned implementation, not just a citation: name the "
            "existing code path to extend and the exact behavior that must change. "
            "Before requesting clarification, finish a read-only discovery sweep of "
            "all affected layers: full authorization lifecycle, field calculations, "
            "null/default behavior, persistence/write paths, and UI precedents. "
            "Continue independent investigation after finding the first ambiguity. "
            "Collect all currently discoverable product decisions into one "
            "needs_human response with a questions array of strings. For each question "
            "give the existing behavior, the conflict, and a recommendation. Do not "
            "ask serially about decisions that can be investigated together. "
            "Planning returns the PlanSpec or needs_human, not implementation_status. "
            "Plan appropriate tests, but test execution is not an approval or handoff gate."
        )

    def review_instructions(self) -> str:
        return (
            self.implementation_instructions()
            + "\nReview independently against the canonical Jira requirements and "
            "approved PlanSpec as clarified by accumulated human decisions for correctness, regressions, minimal scope, and reuse "
            "of established code patterns. Inspect nearby implementations and the "
            "merge-base diff; request deletion or simplification of unnecessary custom "
            "logic. For each pattern or scope finding, cite the changed location, "
            "the applicable existing precedent or requirement, and the smallest "
            "concrete correction. Reject unnecessary new abstractions and behavior; "
            "do not demand a redesign, stylistic rewrite, or speculative hardening. "
            "An explicitly approved necessary deviation is not a defect solely "
            "because it is new. Missing execution evidence alone is residual risk, not a reason "
            "to withhold approval. Concrete code defects still require correction. "
            "Use plan_changes_required if the approved PlanSpec must change. "
            "Do not flag an explicit human override accepted before approval as a defect merely because Jira differs. "
            "This workflow has one approved PlanSpec."
            " Review returns the review decision contract, not implementation_status."
        )

    def validate_plan(self, plan: PlanSpec) -> None:
        outside = set(plan.affected_surface.repositories) - set(self.repositories)
        if self.repositories and outside:
            raise PlanSpecError(
                "PlanSpec includes repositories outside this workflow: "
                + ", ".join(sorted(outside))
            )
