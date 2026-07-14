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
  plan_before_implementation: true
  require_plan_approval: true
  planning_prompt: |
    Inspect the relevant repo areas and write a concise implementation plan/spec.
    Pay extra attention to report/table behavior, translations, API compatibility, persistence/schema behavior, backward compatibility, and which repo owns the change.
    Before proposing implementation:
    - Compare the issue branch against its merge base. Do not treat code already added on the issue branch as an established repository pattern.
    - Inspect the target file and at least two nearby implementations of the same UI or API behavior. Cite those precedents in the plan.
    - Prefer the existing local pattern. Any new renderer, helper, component flag, special-case reset, or persistence behavior must explain why existing patterns are insufficient.
    - Separate explicit Jira requirements from inferred behavior. If an inference changes reset, saved-filter, default, persistence, or compatibility semantics, request clarification instead of implementing it.
    - For filters, document expected behavior for initial load, saved-filter application, manual clearing, Reset Filters, and page reload.
    - Include a "simplest implementation considered" section.
  review_after_run: true
  max_review_iterations: 10
  review_prompt: |
    Review the code changes for this Jira issue independently from both the implementation and the approved plan; the plan may be wrong.
    Make sure that the implementation accounts for all the requirements, including the edge cases identified in the plan/spec file created during the planning phase.
    Independently verify the plan against the Jira requirements and repository conventions.
    - Inventory newly introduced helpers, renderers, flags, and special cases.
    - Search the target component and nearby components for the established pattern.
    - Flag one-off code when the standard pattern satisfies the requirement.
    - Perform a deletion and simplification check: determine whether removing custom logic and relying on component defaults produces the required behavior.
    - Compare behavior against the merge base, not merely the current issue branch.
    - Do not approve behavioral UI changes without a targeted test or documented manual verification of the affected state transitions.
    Also, ensure that the code changes are not doing more than what's asked for. If there is an unnecessary change, add a feedback accordingly to make it a minimal but relevant change.
    Return JSON with:
    - decision: "approve" or "changes_required"
    - findings: a list of concrete findings
    - residual_risk: a short risk summary
    To run tests, go to "/home/adkuppa/compost/" and run "podman compose run foyr bash" or "podman compose run cpm bash" and you'll have the bash environment. podman compose up cpm and podman compose up foyr will start those services if they are not already running.
    Before you handoff, make sure the changes made are working and not causing api or ui failures.
    Focus on correctness, regressions, missing tests, and translation consistency.
---

You are working on Jira issue {{ issue.identifier }}.

Title: {{ issue.title }}
Status: {{ issue.status }}
Priority: {{ issue.priority or "unknown" }}
URL: {{ issue.url }}

Description:
{{ issue.description or "No description provided." }}

Comments:
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
- Do not assume anything and ask questions if you're confused.
- Keep unrelated refactors out of scope.
- Identify unstated edge cases, but do not invent behavior for them. Follow an established precedent or request clarification when the choice changes user-visible semantics.
- Leave a concise final report with files changed, verification, and residual risk.
- Run the configured verification command.
- To run tests, go to "/home/adkuppa/compost/" and run "podman compose run foyr bash" or "podman compose run cpm bash" and you'll have the bash environment. podman compose up cpm and podman compose up foyr will start those services if they are not already running.
- Before you handoff, make sure the changes you made are working and not causing api or ui failures.
