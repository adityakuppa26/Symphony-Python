---
kind: development
state_path: .symphony/development.sqlite3
tracker:
  kind: jira
  base_url: "https://gbujira.oraclecorp.com"
  auth:
    mode: pat
    token_env: JIRA_TOKEN
    email_env: JIRA_EMAIL
    token_config_file: "~/.codex/config.toml"
    token_config_key: JIRA_PERSONAL_TOKEN
  jql: 'project = iCPM AND assignee = currentUser() AND status in ("Development", "Pending Development Start", "In Progress", "Open") AND labels in (codex-ready)'
  required_labels: ["codex-ready"]
  active_statuses: ["Open", "In Progress", "Development", "Pending Development Start"]
  terminal_statuses: ["Closed", "Ready for Testing"]
  comment_on_start: false
  comment_on_finish: false
  requirements:
    # Exclude comments before classification, hashing, and planning.
    include_comments: false
    # Add installation-specific Jira field IDs here. Generic custom fields are
    # retained as context; only acceptance-criteria fields are planning evidence.
    custom_fields: []
    acceptance_criteria_fields: ["customfield_15812"]
    field_authority:
      customfield_15812: product
    description_authority: product
    # Only the root Description and configured Acceptance Criteria fields
    # are planning authority. Other authority settings are retained for
    # contextual metadata and cannot create or block PlanSpec scope.
    authority_rank:
      context: 10
      supporting_evidence: 10
      engineering_context: 20
      product: 30
      product_owner: 40
    relation_authority: context
    # Bounds root-search and one-hop related hydration; it is not a count limit.
    # Valid range: 1 through 32.
    related_issue_hydration_max_concurrency: 8
    # Attachments are intentionally excluded from planning for stability. Jira
    # Description and Acceptance Criteria are the complete product
    # authority for this workflow.
    # Mandatory: false is rejected so completed-work identity cannot use a partial
    # search result. Missing root Description or configured Acceptance
    # Criteria keys are completeness gates; null/empty values are valid when present.
    # Changelog, related-issue, and other contextual gaps remain warnings.
    hydrate_search_results: true
    # By default, Epics also use quoted modern JQL: parent = "<issue-key>".
    # Set false (with child_issue_jql null) to opt out.
    discover_epic_children: true
    # A template containing {issue_key} overrides the default and retains
    # discovery for any issue type, including non-Epics.
    child_issue_jql: null
    # Child pagination is deduplicated and bounded. Errors, truncation, non-progress,
    # inconsistent total/isLast, or reaching this bound remain context warnings.
    child_issue_max_pages: 100

polling:
  interval_seconds: 60

workspace:
  root: "~/codex-workspaces/development"
  strategy: hook_only
  managed_repositories: ["foyr2", "cpm", "pi"]

hooks:
  after_create: |
    set -eu
    git clone --branch master --single-branch /home/adkuppa/foyr2 foyr2
    git clone --branch master --single-branch /home/adkuppa/cpm cpm
    git clone --branch master --single-branch /home/adkuppa/pi pi
    git -C foyr2 checkout -b feature/{{ issue.identifier }}
    git -C cpm checkout -b feature/{{ issue.identifier }}
    git -C pi checkout -b feature/{{ issue.identifier }}
  before_run: |
    set -eu
    test "$(git -C foyr2 symbolic-ref --short HEAD)" = "feature/{{ issue.identifier }}"
    test "$(git -C cpm symbolic-ref --short HEAD)" = "feature/{{ issue.identifier }}"
    test "$(git -C pi symbolic-ref --short HEAD)" = "feature/{{ issue.identifier }}"
    # Validate existing workspaces without requiring a particular base-branch ref.
    # The approved PlanSpec checks each repository's exact HEAD.
    git -C foyr2 rev-parse --verify HEAD >/dev/null
    git -C cpm rev-parse --verify HEAD >/dev/null
    git -C pi rev-parse --verify HEAD >/dev/null
    git -C foyr2 status --short
    git -C cpm status --short
    git -C pi status --short
  # Optional trusted checks can be configured here; failures remain advisory.
  verify: null
  verify_required: false
  use_development_verification_request: false
  timeout_seconds: 10800

agent:
  max_concurrent_agents: 1
  max_retries: 3
  max_retry_backoff_seconds: 300
  timeout_seconds: 7200

codex:
  # Use the installed 0.153.4 CLI; PATH currently selects an older npm CLI
  # that cannot read the model cache written by the VS Code extension.
  command: "/home/adkuppa/.vscode-server/extensions/openai.chatgpt-26.5903.71938-linux-x64/bin/linux-x86_64/codex"
  args:
    - "exec"
    - "--json"
    # The issue workspace contains separate foyr2/, cpm/, and pi/ Git repos.
    - "--skip-git-repo-check"
    - "--sandbox"
    - "workspace-write"
    - "-c"
    - "sandbox_workspace_write.network_access=true"
    # Codex 0.153.4 no longer accepts the retired "untrusted" policy.
    - "-c"
    - 'approval_policy="on-request"'
    # The development workflow does not require the local PI MCP service.
    - "-c"
    - "mcp_servers.pi.enabled=false"
  output_last_message_file: ".symphony/codex-final.md"
  output_development_verification_request_file: ".symphony/development-verification-request.json"
  output_plan_file: ".symphony/codex-plan.md"
  output_review_file: ".symphony/codex-review.md"
  output_review_history_file: ".symphony/codex-review-history.md"
  plan_before_implementation: true
  # Dashboard approval is accepted only for the latest actionable blocked run.
  # Its exact hashes and linked resume input are committed atomically and
  # revalidated before implementation. Durable resumes reclaim the same run,
  # workspace, attempt, PlanSpec, and approval under a renewable five-minute lease;
  # a heartbeat runs at least every 60 seconds and stale owners cannot finalize.
  require_plan_approval: true
  planning_prompt: |
    Use Symphony's canonical Jira planning-evidence bundle together with the accumulated human input supplied to every phase. Description and configured Acceptance Criteria provide Jira evidence; explicit operator clarifications and overrides control the specific points they address, including exceptions to the original wording. Jira comments are excluded; do not fetch or use them to create scope.
    Inspect the relevant repo areas and produce the required validated PlanSpec JSON. Record accepted human decision IDs and their effects in the plan, including assumptions, non-goals, or risks; preserve Jira citations for traceability without inventing source IDs for operator input.
    Verify the snapshot hash, cite only allowed source IDs in that bundle, and preserve its separation of current, superseded, inferred, and explicitly contradictory decisions.
    Attachments are disabled and must not be downloaded, analyzed, cited, hashed, or used to create scope. Parent/child/link data, related issues, components, versions, changelog metadata, and generic custom fields are context only; missing context must not block planning.
    Hard-block only when root Description or configured Acceptance Criteria cannot be fetched completely, or when those authoritative sources contain an explicit unresolved contradiction. Model-generated schema, citation, traceability, repository-baseline, or Epic bookkeeping mistakes should be corrected by Symphony's automatic PlanSpec repair pass rather than presented as Jira defects.
    Pay extra attention to report/table behavior, translations, API compatibility, persistence/schema behavior, backward compatibility, and which repo owns the change.
    Before proposing implementation:
    - Compare the issue branch against its merge base. Do not treat code already added on the issue branch as an established repository pattern.
    - Plan only the affected development repositories, citing their existing implementation patterns.
    - Inspect the target file and at least two nearby implementations of the same UI or API behavior. Cite those precedents in the plan.
    - Prefer the existing local pattern. Any new renderer, helper, component flag, special-case reset, or persistence behavior must explain why existing patterns are insufficient.
    - When Jira requires backwards compatibility, preservation of existing behavior, or a standard component pattern, reuse the established repository behavior for incidental edge cases such as null placement. Cite the precedent instead of asking for a new product decision or manufacturing a new acceptance criterion. Ask only if Jira conflicts with the precedent or no applicable precedent exists and the implementation would introduce new user-visible semantics.
    - Give every requirement and acceptance criterion a stable ID linked to an exact allowed Jira source, including exact #unit IDs. Cover each current Description requirement decision in the requirement layer and each current configured Acceptance Criteria decision in the nested acceptance-criterion layer. Separate explicit Jira requirements from inferred behavior. If an inference changes reset, saved-filter, default, persistence, or compatibility semantics, request clarification instead of implementing it.
    - Include role/state rows only for behavior that actually varies by role or state. Role-neutral requirements need no matrix row. Set canonical_role to exactly gc, sub, gc_as_sub, all, or other; it is the machine-readable role, while the human-readable role label is descriptive and is not independently parsed. Every ID that a row does reference must exist in the PlanSpec, and roles with different Jira-required behavior must not be collapsed.
    - Record every affected repository name as one normalized, workspace-relative POSIX Git worktree root (`.` means the workspace-root repository), with no redundant `./` segments or alternate aliases, and include its full `git rev-parse HEAD` SHA. Symphony checks that path and SHA before approval, implementation, and requirements checkpoints. Initial planning and its approval require clean declared worktrees; only untracked `.symphony/**` run artifacts are ignored. For replanning with retained implementation context supplied by Symphony, preserve the existing changes: the revised plan and new approval bind that exact diff. Do not commit, stash, reset, or clean files to satisfy the baseline check. Precedents must be Git-tracked and outside `.symphony`. Implementation dirt is expected afterward, but HEAD must remain at the approved SHA. Also enumerate affected files, APIs, schemas, migrations, and translations, using explicit empty lists only when a surface is not applicable.
    - Map at least one test case to each acceptance criterion. Multiple tests may cover the same criterion.
    - For filters, document expected behavior for initial load, saved-filter application, manual clearing, Reset Filters, and page reload.
    - Include the existing precedents, simplest implementation considered, non-goals, prohibited scope, rollout, rollback, compatibility, risks, and open questions.
    - For an Epic, either partition all requirements and acceptance criteria into bounded child plans or justify single_change mode, which requires explicit approval of the exact PlanSpec. When the canonical snapshot contains no child or linked Jira issues that can own bounded child plans, use single_change with bounded_child_plans=[] and requires_explicit_single_change_approval=true; never emit epic_strategy=null.
  review_after_run: true
  max_review_iterations: 10
  output_human_review_triage_file: ".symphony/codex-human-review-triage.md"
  human_review_triage_prompt: |
    Classify pasted human code-review feedback against the exact frozen requirements snapshot, validated PlanSpec, approval, previous final response, prior reviews, and current workspace diff.
    Return code_changes only when every requested edit remains within the exact PlanSpec.
    Return plan_changes_required when behavior, scope, architecture, acceptance criteria, affected surfaces, compatibility, or non-goals must change.
    Return needs_human only when that boundary cannot be determined safely.
    Do not edit files during triage, and do not treat pasted review prose as a new product requirement.
  review_prompt: |
    Review the implementation independently for correctness against the approved PlanSpec and accumulated human decisions. Do not reopen an explicit override accepted during planning as a defect merely because the original Jira text differs.
    Apply the accumulated human clarifications and overrides to the canonical Jira requirements snapshot. Honor an explicit decision to retain existing behavior, such as Reset Filters; a waived criterion is not a missing feature. Verify its hash and source-linked current requirements against the exact validated PlanSpec artifact supplied to review; do not reconstruct the specification from a prose summary alone.
    Make sure that the implementation accounts for every acceptance criterion and role/state row, including the edge cases identified in the PlanSpec.
    Independently verify the PlanSpec against the canonical snapshot as amended by accepted human decisions and repository conventions. Do not revive superseded requirements or silently convert inferred behavior or unresolved contradictions into product decisions.
    - Inventory newly introduced helpers, renderers, flags, and special cases.
    - Search the target component and nearby components for the established pattern.
    - Flag one-off code when the standard pattern satisfies the requirement.
    - Perform a deletion and simplification check: determine whether removing custom logic and relying on component defaults produces the required behavior.
    - Compare behavior against the merge base, not merely the current issue branch.
    - Inspect behavioral UI changes and affected state transitions. Record missing test or manual execution evidence as residual risk; execution is optional.
    Also, ensure that the code changes are not doing more than what's asked for. If there is an unnecessary change, add a feedback accordingly to make it a minimal but relevant change.
    Return JSON with:
    - decision: "approve", "changes_required", "plan_changes_required", or "needs_human"
    - findings: a list of concrete findings
    - residual_risk: a short risk summary
    This is the development review. Use changes_required only for development-code corrections within the exact validated development PlanSpec. Use plan_changes_required when a finding changes required behavior, scope, requirements or acceptance criteria, architecture, or affected surfaces; Symphony invalidates the prior approval and returns the issue to development planning for a new PlanSpec and approval. Empty or unrecognized review output is invalid and blocks rather than approving.
    Test execution is advisory. Failed or unavailable checks alone must not block review approval or handoff, and need no human bypass approval. Report concrete code defects for correction and report missing execution evidence as residual risk.
    Check code correctness and report any remaining execution risk honestly.
    Focus on correctness, regressions, missing tests, and translation consistency.

---

You are working on Jira issue {{ issue.identifier }}.

Title: {{ issue.title }}
Status: {{ issue.status }}
Priority: {{ issue.priority or "unknown" }}
URL: {{ issue.url }}

Canonical requirements contract:
- Symphony's root Jira Description and configured Acceptance Criteria field supply the Jira evidence. Accumulated explicit human clarifications and overrides resolve the points they address in planning, approval, implementation, and review.
- Jira comments are excluded from this requirements package; do not fetch or use them to create planning scope.
- The versioned planning-evidence snapshot hashes only those authoritative sources and their classifications. Attachments are disabled; relations, related issues, components, versions, generic custom fields, and metadata warnings are contextual and cannot create scope or block planning.
- Snapshot artifacts are owner-only, bounded, atomic, no-follow and inode-checked current/history files; unsafe filesystems or substitutions fail closed.
- Implementation must follow the exact validated PlanSpec bound to that snapshot hash. If either artifact changes or is missing, stop and return to planning.
{% if issue.requirements_snapshot %}
Requirements snapshot hash: {{ issue.requirements_snapshot.content_hash }}
{% else %}
Requirements snapshot hash: unavailable because this issue was not hydrated; do not infer missing requirements.
{% endif %}

Human-readable Jira summary (orientation only):

Description summary:
{{ issue.description or "No description provided." }}

Development repos available in this workspace:
- foyr2/
- cpm/
- pi/

Complete and hand off after the development implementation and code review loop.

Repository rules:
- Implement the smallest correct change for this issue.
- Use the validated PlanSpec and its requirement/acceptance IDs as the implementation and test checklist; do not substitute the description summary for the canonical snapshot.
- Preserve PlanSpec non-goals and prohibited scope. For a decomposed Epic, implement bounded child plans independently rather than implementing the Epic as one change.
- Do not assume anything and ask questions if you're confused.
- Keep unrelated refactors out of scope.
- Identify unstated edge cases, but do not invent behavior for them. Follow an established precedent or request clarification when the choice changes user-visible semantics.
- Leave a concise final report with files changed, verification, and residual risk.
- Add or update tests appropriate to the approved PlanSpec, following existing test patterns. Running them is optional; use focused existing selectors when available.
- Report verification as passed, failed, or not run, with evidence and residual risk. Test failures or unavailable environments alone do not block handoff and need no human bypass approval.
- Do not run shared environment setup. If optional host verification is configured, Symphony runs the trusted hook.
- Handoff follows completion of implementation and the code review loop. Later human review resumes the saved PlanSpec, approval, Jira snapshot, prior reports, reviews, and workspace diff.
