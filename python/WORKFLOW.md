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
  jql: 'project = iCPM AND assignee = currentUser() AND status in ("Development", "Pending Development Start", "In Progress", "Open") AND labels = codex-ready'
  required_labels: ["codex-ready"]
  active_statuses: ["Open", "In Progress", "Development", "Pending Development Start"]
  terminal_statuses: ["Closed", "Ready for Testing"]
  comment_on_start: true
  comment_on_finish: true

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
  max_retries: 2
  max_retry_backoff_seconds: 300
  timeout_seconds: 7200

codex:
  command: "codex"
  args:
    - "exec"
    - "--json"
    - "--sandbox"
    - "workspace-write"
  output_last_message_file: ".symphony/codex-final.md"
  plan_before_implementation: true
  require_plan_approval: true
  planning_prompt: |
    Inspect the relevant repo areas and write a concise implementation plan/spec.
    Pay extra attention to report/table behavior, translations, API compatibility, persistence/schema behavior, backward compatibility, and which repo owns the change.
  review_after_run: true
  max_review_iterations: 10
  review_prompt: |
    Review the code changes for this Jira issue.
    Return JSON with:
    - decision: "approve" or "changes_required"
    - findings: a list of concrete findings
    - residual_risk: a short risk summary
    To run tests, go to "/home/adkuppa/compost/" and run "docker-compose run foyr bash" or "docker-compose run cpm bash" and you'll have the environment. 
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
- Leave a concise final report with files changed, verification, and residual risk.
- Run the configured verification command.
- To run tests, go to "/home/adkuppa/compost/" and run "docker-compose run foyr bash" or "docker-compose run cpm bash" and you'll have the environment.
- Before you handoff, make sure the changes you made are working and not causing api or ui failures.
