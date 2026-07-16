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
    # Add installation-specific Jira field IDs here. Acceptance-criteria fields
    # are also included in the general custom-field requirements bundle.
    custom_fields: []
    acceptance_criteria_fields: []
    field_authority: {}
    description_authority: product
    comment_authority: product
    # Exact display-name/email/username matches are trimmed and case-insensitive.
    # Edited comments use updateAuthor and updated time for the current body.
    comment_authority_by_author: {}
    # Every description/comment/author/field/attachment/relation authority must
    # be nonblank and listed here or configuration validation fails.
    authority_rank:
      context: 10
      supporting_evidence: 10
      engineering_context: 20
      product: 30
      product_owner: 40
    attachment_authority: supporting_evidence
    relation_authority: context
    comment_page_size: 100
    # Bounds root-search and one-hop related hydration; it is not a count limit.
    # Valid range: 1 through 32.
    related_issue_hydration_max_concurrency: 8
    download_attachments: true
    max_attachment_bytes: 10485760
    # Content URLs must resolve to the exact Jira origin. Bytes are streamed and
    # aborted above max_attachment_bytes; downloads never send Jira auth off-origin.
    # Valid concurrency range: 1 through 32.
    attachment_download_max_concurrency: 4
    require_attachment_analysis: true
    # Codex mode sends screenshots and rendered PDF pages to the model provider
    # configured for the locally authenticated Codex CLI. Enable it only after
    # that provider is approved for the Jira evidence's data-handling class.
    attachment_analyzer: codex
    attachment_analysis_timeout_seconds: 120
    attachment_pdf_max_pages: 8
    attachment_analysis_max_concurrency: 1
    attachment_analysis_max_output_characters: 12000
    # Mandatory: false is rejected so completed-work identity cannot use a partial
    # search result. Missing core/configured fields and absent/malformed changelog
    # provenance are completeness gates; null/empty values are valid when present.
    hydrate_search_results: true
    # By default, Epics also use quoted modern JQL: parent = "<issue-key>".
    # Set false (with child_issue_jql null) to opt out.
    discover_epic_children: true
    # A template containing {issue_key} overrides the default and retains
    # discovery for any issue type, including non-Epics.
    child_issue_jql: null
    # Child pagination is deduplicated and fails incomplete on errors, truncation,
    # non-progress, inconsistent total/isLast, or this bound. Maximum: 1000.
    child_issue_max_pages: 100

polling:
  interval_seconds: 60

workspace:
  root: "~/codex-workspaces"
  strategy: hook_only

hooks:
  after_create: |
    git clone /home/adkuppa/foyr2 foyr2
    git clone /home/adkuppa/cpm cpm
    git clone /home/adkuppa/pi pi
  before_run: |
    git -C foyr2 status --short
    git -C cpm status --short
    git -C pi status --short
  # Advisory: failure is recorded and surfaced, but does not block completion.
  verify: |
    git -C foyr2 diff --check
    git -C cpm diff --check
    git -C pi diff --check

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
    - "--add-dir"
    - "/home/adkuppa/compost"
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
    Treat Symphony's canonical, versioned Jira requirements snapshot as the authoritative product input. The description and comments in the human-readable workflow prompt are orientation only and are not a complete specification.
    Inspect the relevant repo areas and produce the required validated PlanSpec JSON.
    Verify the snapshot hash, use only source IDs present in that snapshot, and preserve its explicit separation of current requirements, superseded requirements, inferred behavior, and unresolved contradictions.
    New snapshots use jira-requirements/v2. Mixed clauses and bullets have exact digest-stable #unit source IDs. Jira authors may use [classification: current], [inferred], [superseded], [contradiction], and [supersedes: jira:KEY:artifact-or-unit-id]. Ambiguous replacement prose, lower/unranked overrides, cycles, and clear polarity/order conflicts remain unresolved rather than silently changing scope.
    Complete attachment summaries enter the same taxonomy as supporting evidence. Only an exact current attachment decision unit may anchor a PlanRequirement; inferred, superseded, or contradictory units cannot. Attachment section labels never manufacture acceptance criteria.
    Symphony hard-blocks before this planning prompt whenever incomplete_reasons is non-empty. Missing requested root/related field keys (distinct from present null/empty values), missing OCR/vision analysis, off-origin or failed attachment downloads, truncated comments, absent/malformed changelog provenance or total, unavailable exact source author/timestamp, blank/unranked authority, and unresolved Jira contradictions must be corrected in Jira or the evidence pipeline and then refetched; dashboard clarification text cannot waive this source-provenance gate.
    Pay extra attention to report/table behavior, translations, API compatibility, persistence/schema behavior, backward compatibility, and which repo owns the change.
    Before proposing implementation:
    - Compare the issue branch against its merge base. Do not treat code already added on the issue branch as an established repository pattern.
    - Inspect the target file and at least two nearby implementations of the same UI or API behavior. Cite those precedents in the plan.
    - Prefer the existing local pattern. Any new renderer, helper, component flag, special-case reset, or persistence behavior must explain why existing patterns are insufficient.
    - Give every requirement and acceptance criterion a stable ID linked to its exact Jira issue identifier, source type, and source ID, including exact #unit IDs and related-issue attachment evidence. Cover every current requirement source in the requirement layer and every current acceptance-criterion source in its matching nested layer. Every PlanRequirement and AcceptanceCriterion must cite a current decision source, and every completely analyzed root or related attachment must be cited by active scope. Separate explicit Jira requirements from inferred behavior. If an inference changes reset, saved-filter, default, persistence, or compatibility semantics, request clarification instead of implementing it.
    - Include every relevant role/state combination in the role/state matrix, and reference every planned requirement and acceptance-criterion ID in at least one row. Set canonical_role to exactly gc, sub, gc_as_sub, all, or other, and keep the human-readable role display label separate; role-specific labels must identify only their matching canonical role and cannot be negated/exclusionary. Do not collapse GC, Sub, and GC-acting-as-Sub when their behavior or evidence differs. A role explicitly absent/not shown in only a complete attachment summary does not require a row; a current Jira decision that the role is not applicable still requires its own state row.
    - Record every affected repository name as a workspace-relative Git worktree root (`.` means the workspace-root repository) and its full `git rev-parse HEAD` SHA. Symphony checks that path and SHA before approval, implementation, and requirements checkpoints. Initial planning and approval require clean declared worktrees; only untracked `.symphony/**` run artifacts are ignored. Precedents must be Git-tracked and outside `.symphony`. Implementation dirt is expected afterward, but HEAD must remain at the approved SHA. Also enumerate affected files, APIs, schemas, migrations, and translations, using explicit empty lists only when a surface is not applicable.
    - Map exactly one test case to each acceptance criterion.
    - For filters, document expected behavior for initial load, saved-filter application, manual clearing, Reset Filters, and page reload.
    - Include the existing precedents, simplest implementation considered, non-goals, prohibited scope, rollout, rollback, compatibility, risks, and open questions.
    - For an Epic, either partition all requirements and acceptance criteria into bounded child plans or justify single_change mode, which requires explicit approval of the exact PlanSpec.
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
    - decision: "approve", "changes_required", or "plan_changes_required"
    - findings: a list of concrete findings
    - residual_risk: a short risk summary
    Use changes_required only for code changes that remain within the exact validated PlanSpec. Use plan_changes_required when a finding changes required behavior, scope, requirements or acceptance criteria, architecture, or affected surfaces; Symphony invalidates the prior approval and returns the issue to planning for a new PlanSpec and approval. Empty or unrecognized review output is invalid and blocks rather than approving.
    - To run tests,
      1. Update .env file under /home/adkuppa/compost/. Set the CPM_SRC, FOYR_SRC, PI_SRC variables to the right directory under /home/adkuppa/codex-workspaces/.
      2. Make sure the relevant services are running. If not, "podman compose up <service_name>" will start the service. Example, podman compose up cpm.
      3. Run "podman compose run foyr bash" or "podman compose run cpm bash" and you'll have the bash environment.
      4. Use pytest to run the unit tests.
    Before you handoff, make sure the changes made are working and not causing api or ui failures.
    Focus on correctness, regressions, missing tests, and translation consistency.
---

You are working on Jira issue {{ issue.identifier }}.

Title: {{ issue.title }}
Status: {{ issue.status }}
Priority: {{ issue.priority or "unknown" }}
URL: {{ issue.url }}

Canonical requirements contract:
- Symphony's hydrated requirements snapshot is authoritative for planning, approval, implementation, and review.
- The snapshot includes configured fields, full paginated comments, attachments and analyses, relations, components, versions, provenance, authority, classifications, incomplete reasons, and a stable content hash.
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

Repos available in this workspace:
- foyr2/
- cpm/
- pi/

Make changes in whichever repo is required by the Jira issue.

Repository rules:
- Implement the smallest correct change for this issue.
- Use the validated PlanSpec and its requirement/acceptance IDs as the implementation and test checklist; do not substitute the description/comment summary for the canonical snapshot.
- Preserve PlanSpec non-goals and prohibited scope. For a decomposed Epic, implement bounded child plans independently rather than implementing the Epic as one change.
- Do not assume anything and ask questions if you're confused.
- Keep unrelated refactors out of scope.
- Identify unstated edge cases, but do not invent behavior for them. Follow an established precedent or request clarification when the choice changes user-visible semantics.
- Leave a concise final report with files changed, verification, and residual risk.
- Attempt the configured verification command. If the stack is unavailable or the command fails, report the limitation and residual risk, then continue; the orchestrator records the hook result as advisory.
- To run tests,
  1. Update .env file under /home/adkuppa/compost/. Set the CPM_SRC, FOYR_SRC, PI_SRC variables to the right directory under /home/adkuppa/codex-workspaces/.
  2. Make sure the relevant services are running. If not, "podman compose up <service_name>" will start the service. Example, podman compose up cpm.
  3. Run "podman compose run foyr bash" or "podman compose run cpm bash" and you'll have the bash environment.
  4. Use pytest to run the unit tests.
- Before you handoff, make sure the changes you made are working and not causing api or ui failures.
