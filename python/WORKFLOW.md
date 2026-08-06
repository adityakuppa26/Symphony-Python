---
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
    # Add installation-specific Jira field IDs here. Generic custom fields are
    # retained as context; only acceptance-criteria fields are planning evidence.
    custom_fields: []
    acceptance_criteria_fields: ["customfield_15812"]
    field_authority:
      customfield_15812: product
    description_authority: product
    comment_authority: product
    # Exact display-name/email/username matches are trimmed and case-insensitive.
    # Edited comments use updateAuthor and updated time for the current body.
    comment_authority_by_author: {}
    # Only the root Description, configured Acceptance Criteria fields, and root
    # comments are planning authority. Other authority settings are retained for
    # contextual metadata and cannot create or block PlanSpec scope.
    authority_rank:
      context: 10
      supporting_evidence: 10
      engineering_context: 20
      product: 30
      product_owner: 40
    relation_authority: context
    comment_page_size: 100
    # Bounds root-search and one-hop related hydration; it is not a count limit.
    # Valid range: 1 through 32.
    related_issue_hydration_max_concurrency: 8
    # Attachments are intentionally excluded from planning for stability. Jira
    # Description, Acceptance Criteria, and comments are the complete product
    # authority for this workflow.
    # Mandatory: false is rejected so completed-work identity cannot use a partial
    # search result. Missing root Description/comment or configured Acceptance
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
  root: "~/codex-workspaces"
  strategy: hook_only

hooks:
  after_create: |
    set -eu
    git clone --branch develop --single-branch /home/adkuppa/foyr2 foyr2
    git clone --branch develop --single-branch /home/adkuppa/cpm cpm
    git clone --branch develop --single-branch /home/adkuppa/pi pi
    git clone --no-hardlinks --branch master --single-branch /home/adkuppa/CPM automation
    git -C automation remote remove origin
    git -C foyr2 checkout -b feature/{{ issue.identifier }}
    git -C cpm checkout -b feature/{{ issue.identifier }}
    git -C pi checkout -b feature/{{ issue.identifier }}
    git -C automation checkout -b "{{ issue.identifier }}"
  before_run: |
    set -eu
    test ! -L automation
    if [ ! -e automation ]; then
      git clone --no-hardlinks --branch master --single-branch /home/adkuppa/CPM automation
      git -C automation remote remove origin
      git -C automation checkout -b "{{ issue.identifier }}"
    fi
    test -d automation/.git
    test ! -L automation/.git
    if git -C automation remote get-url origin >/dev/null 2>&1; then
      git -C automation remote remove origin
    fi
    test -z "$(git -C automation remote)"
    test "$(git -C foyr2 symbolic-ref --short HEAD)" = "feature/{{ issue.identifier }}"
    test "$(git -C cpm symbolic-ref --short HEAD)" = "feature/{{ issue.identifier }}"
    test "$(git -C pi symbolic-ref --short HEAD)" = "feature/{{ issue.identifier }}"
    test "$(git -C automation symbolic-ref --short HEAD)" = "{{ issue.identifier }}"
    git -C foyr2 show-ref --verify --quiet refs/heads/develop
    git -C cpm show-ref --verify --quiet refs/heads/develop
    git -C pi show-ref --verify --quiet refs/heads/develop
    git -C automation show-ref --verify --quiet refs/heads/master
    git -C foyr2 status --short
    git -C cpm status --short
    git -C pi status --short
    git -C automation status --short
  verify: |
    git -C foyr2 diff --check
    git -C cpm diff --check
    git -C pi diff --check
    git -C automation diff --check
  verify_required: true

runtime:
  kind: podman_compose
  enabled: true
  required: true
  shutdown_after_handoff: true
  shutdown_grace_seconds: 120
  command: ["/usr/bin/podman", "compose"]
  project_directory: "/home/adkuppa/compost"
  compose_file: "/home/adkuppa/compost/docker-compose.yml"
  env_file: "/home/adkuppa/compost/.env"
  project_name: "compost"
  lock_file: "~/.local/state/symphony/compost-runtime.lock"
  repositories:
    foyr2:
      workspace_subdir: "foyr2"
      source_env: "FOYR_SRC"
      service: "foyr"
      mount_target: "/src"
      dependencies: ["ibis"]
      force_recreate_dependencies: ["ibis"]
      container_workdir: "/src"
      verification_profile: "foyr_pytest"
    cpm:
      workspace_subdir: "cpm"
      source_env: "CPM_SRC"
      service: "cpm"
      mount_target: "/TexturaWD/textura"
      dependencies: ["oracledb19", "memcached"]
      container_workdir: "/TexturaWD/textura"
      verification_profile: "cpm_pytest"
    pi:
      workspace_subdir: "pi"
      source_env: "PI_SRC"
      service: "pi"
      mount_target: "/pi"
      dependencies: ["oracledb23ai"]
      container_workdir: "/pi"
      verification_profile: "pi_pytest"
  verification_profiles:
    cpm_pytest:
      argv: ["pytest"]
      default_args: ["Test/unit"]
      timeout_seconds: 3600
    foyr_pytest:
      argv: ["pytest"]
      default_args:
        - "/src/tests"
        - "-n"
        - "4"
        - "--tb=native"
        - "--junitxml=/src/pytest-results.xml"
      environment:
        FOYR_CONFIG_FILE: "/src/tests/testing.yml"
      timeout_seconds: 3600
    pi_pytest:
      argv: ["hatch", "run", "dev:test"]
      timeout_seconds: 3600

agent:
  max_concurrent_agents: 1
  max_retries: 3
  max_retry_backoff_seconds: 300
  timeout_seconds: 7200

codex:
  command: "codex"
  args:
    - "exec"
    - "--json"
    - "--sandbox"
    - "workspace-write"
    - "-c"
    - "sandbox_workspace_write.network_access=true"
    - "-c"
    - 'approval_policy="untrusted"'
  output_last_message_file: ".symphony/codex-final.md"
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
    Treat Symphony's canonical, versioned Jira planning-evidence bundle as the authoritative product input. Only the root issue Description, configured Acceptance Criteria field, and root issue comments may define requirements or acceptance criteria.
    Inspect the relevant repo areas and produce the required validated PlanSpec JSON.
    Verify the snapshot hash, cite only allowed source IDs in that bundle, and preserve its separation of current, superseded, inferred, and explicitly contradictory decisions.
    Attachments are disabled and must not be downloaded, analyzed, cited, hashed, or used to create scope. Parent/child/link data, related issues, components, versions, changelog metadata, and generic custom fields are context only; missing context must not block planning.
    Hard-block only when root Description, configured Acceptance Criteria, or root comments cannot be fetched completely, or when those authoritative sources contain an explicit unresolved contradiction. Model-generated schema, citation, traceability, repository-baseline, or Epic bookkeeping mistakes should be corrected by Symphony's automatic PlanSpec repair pass rather than presented as Jira defects.
    Pay extra attention to report/table behavior, translations, API compatibility, persistence/schema behavior, backward compatibility, and which repo owns the change.
    Before proposing implementation:
    - Compare the issue branch against its merge base. Do not treat code already added on the issue branch as an established repository pattern.
    - Keep automation/ out of the development PlanSpec and implementation scope. Symphony plans and applies automation updates in a separate post-development phase using the approved PlanSpec and actual development changes.
    - Inspect the target file and at least two nearby implementations of the same UI or API behavior. Cite those precedents in the plan.
    - Prefer the existing local pattern. Any new renderer, helper, component flag, special-case reset, or persistence behavior must explain why existing patterns are insufficient.
    - When Jira requires backwards compatibility, preservation of existing behavior, or a standard component pattern, reuse the established repository behavior for incidental edge cases such as null placement. Cite the precedent instead of asking for a new product decision or manufacturing a new acceptance criterion. Ask only if Jira conflicts with the precedent or no applicable precedent exists and the implementation would introduce new user-visible semantics.
    - Give every requirement and acceptance criterion a stable ID linked to an exact allowed Jira source, including exact #unit IDs. Cover each current Description/comment requirement decision in the requirement layer and each current configured Acceptance Criteria decision in the nested acceptance-criterion layer. Separate explicit Jira requirements from inferred behavior. If an inference changes reset, saved-filter, default, persistence, or compatibility semantics, request clarification instead of implementing it.
    - Include role/state rows only for behavior that actually varies by role or state. Role-neutral requirements need no matrix row. Set canonical_role to exactly gc, sub, gc_as_sub, all, or other; it is the machine-readable role, while the human-readable role label is descriptive and is not independently parsed. Every ID that a row does reference must exist in the PlanSpec, and roles with different Jira-required behavior must not be collapsed.
    - Record every affected repository name as one normalized, workspace-relative POSIX Git worktree root (`.` means the workspace-root repository), with no redundant `./` segments or alternate aliases, and include its full `git rev-parse HEAD` SHA. Symphony checks that path and SHA before approval, implementation, and requirements checkpoints. Initial planning and approval require clean declared worktrees; only untracked `.symphony/**` run artifacts are ignored. Precedents must be Git-tracked and outside `.symphony`. Implementation dirt is expected afterward, but HEAD must remain at the approved SHA. Also enumerate affected files, APIs, schemas, migrations, and translations, using explicit empty lists only when a surface is not applicable.
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
    Return automation_plan_changes_required when the development PlanSpec remains valid and only the derived automation plan must change.
    Return plan_changes_required when behavior, scope, architecture, acceptance criteria, affected surfaces, compatibility, or non-goals must change.
    Return needs_human only when that boundary cannot be determined safely.
    Do not edit files during triage, and do not treat pasted review prose as a new product requirement.
  review_prompt: |
    Review the code changes for this Jira issue independently from both the implementation and the approved plan; the plan may be wrong.
    Treat the current canonical Jira requirements snapshot as authoritative. Verify its hash and source-linked current requirements against the exact validated PlanSpec artifact supplied to review; do not reconstruct the specification from description and comments alone.
    Make sure that the implementation accounts for every acceptance criterion and role/state row, including the edge cases identified in the PlanSpec.
    Independently verify the PlanSpec against the canonical snapshot and repository conventions. Do not revive superseded requirements or silently convert inferred behavior or unresolved contradictions into product decisions.
    - Inventory newly introduced helpers, renderers, flags, and special cases.
    - Search the target component and nearby components for the established pattern.
    - Flag one-off code when the standard pattern satisfies the requirement.
    - Perform a deletion and simplification check: determine whether removing custom logic and relying on component defaults produces the required behavior.
    - Compare behavior against the merge base, not merely the current issue branch.
    - Do not approve behavioral UI changes without a targeted test or documented manual verification of the affected state transitions.
    Also, ensure that the code changes are not doing more than what's asked for. If there is an unnecessary change, add a feedback accordingly to make it a minimal but relevant change.
    Return JSON with:
    - decision: "approve", "changes_required", "automation_plan_changes_required", or "plan_changes_required"
    - findings: a list of concrete findings
    - residual_risk: a short risk summary
    Use changes_required only for code changes that remain within the exact validated development and automation plans. Use automation_plan_changes_required when the development PlanSpec remains valid and only the derived automation plan must change. Use plan_changes_required when a finding changes required behavior, scope, requirements or acceptance criteria, architecture, or affected surfaces; Symphony invalidates the prior approval and returns the issue to development planning for a new PlanSpec and approval. Empty or unrecognized review output is invalid and blocks rather than approving.
    Symphony owns required runtime verification and supplies its persisted evidence to review. Codex must never edit or invoke /home/adkuppa/compost or Podman.
    Before you handoff, make sure the changes made are working and not causing api or ui failures.
    Focus on correctness, regressions, missing tests, and translation consistency.

automation:
  enabled: true
  workspace_subdir: "automation"
  output_plan_file: ".symphony/codex-automation-plan.md"
  output_result_file: ".symphony/codex-automation-final.md"
  planning_prompt: |
    After the approved development PlanSpec has been implemented, plan only the relevant automation update in automation/.
    Use the canonical Jira requirements, exact approved development PlanSpec, development result, and actual development diff as the behavior contract, then inspect the existing automation code for its established patterns.
    Identify the smallest useful regression or end-to-end coverage change, including the exact automation files and focused checks. Do not edit files during this planning pass.
    If the development change does not warrant an automation-code update, return an explicit no-op plan with a concrete reason; do not manufacture coverage or unrelated cleanup.
  implementation_prompt: |
    Apply the automation plan only in automation/. Keep the change narrowly tied to the Jira requirements, approved development PlanSpec, and actual development implementation, and follow the repository's existing automation patterns.
    Do not edit foyr2/, cpm/, or pi/, and do not add unrelated refactors or speculative coverage.
    If the automation plan is a no-op, leave automation/ unchanged and report the reason.
---

You are working on Jira issue {{ issue.identifier }}.

Title: {{ issue.title }}
Status: {{ issue.status }}
Priority: {{ issue.priority or "unknown" }}
URL: {{ issue.url }}

Canonical requirements contract:
- Symphony's root Jira Description, configured Acceptance Criteria field, and complete root comments are authoritative for planning, approval, implementation, and review.
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

Comment summary:
{% for comment in issue.comments %}
- {{ comment.author }} at {{ comment.created }}: {{ comment.body }}
{% endfor %}

Development repos available in this workspace:
- foyr2/
- cpm/
- pi/

The post-development automation repo is automation/. During development, make
changes only in whichever development repo is required by the Jira issue; Symphony
plans and applies any relevant automation update afterward.

Repository rules:
- Implement the smallest correct change for this issue.
- Use the validated PlanSpec and its requirement/acceptance IDs as the implementation and test checklist; do not substitute the description/comment summary for the canonical snapshot.
- Preserve PlanSpec non-goals and prohibited scope. For a decomposed Epic, implement bounded child plans independently rather than implementing the Epic as one change.
- Do not assume anything and ask questions if you're confused.
- Keep unrelated refactors out of scope.
- Identify unstated edge cases, but do not invent behavior for them. Follow an established precedent or request clarification when the choice changes user-visible semantics.
- Leave a concise final report with files changed, verification, and residual risk.
- Add or update the repository tests required by the validated PlanSpec. Symphony owns required runtime verification after implementation.
- Do not edit automation/ during development. Its separate planning pass uses the canonical requirements, approved development PlanSpec, and resulting development diff. A justified no-op is valid when no automation-code change is relevant.
- Never edit or invoke /home/adkuppa/compost or Podman. Symphony binds this ticket's workspace paths through subprocess environment overrides without changing the shared .env file.
- Before you handoff, make sure the changes you made are working and not causing api or ui failures.
