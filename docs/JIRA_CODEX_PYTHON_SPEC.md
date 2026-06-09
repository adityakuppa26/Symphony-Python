# Jira Codex Python Symphony Specification

Status: implementation handoff spec

Purpose: build a Python implementation of Symphony that pulls work from Jira, creates isolated per-issue workspaces, runs the local Codex CLI using the user's existing Codex authentication, reports results back to Jira, and exposes a local dashboard.

This spec is intended for another Codex session to implement directly.

## Critical Constraints

- Do not require `OPENAI_API_KEY`, `CODEX_API_KEY`, or direct OpenAI API access.
- Use the locally installed `codex` CLI. It should reuse the user's existing Codex login, the same way the user can run Codex from terminal or VS Code.
- The service should run locally on the developer machine first. Remote workers and CI can come later.
- Do not depend on Codex chat-session MCP tools for Jira access. The Python service must use Jira REST APIs through its own configured Jira credentials.
- Keep Jira writes in the Python orchestrator for the MVP. Codex should edit code and produce a final report; the orchestrator should comment on Jira and optionally transition the issue.
- Treat the upstream `SPEC.md` as the architectural reference, but implement only the Python/Jira/Codex-CLI subset described here.

## Goals

- Poll Jira using JQL on a fixed cadence.
- Dispatch eligible Jira issues with bounded concurrency.
- Create and preserve an isolated workspace per Jira issue.
- Bootstrap each workspace from a repository clone, git worktree, or hook.
- Render a per-issue prompt from `WORKFLOW.md`.
- Run Codex non-interactively with `codex exec --json` in the issue workspace.
- Store structured run events, final status, logs, and verification results.
- Post a concise result comment to Jira.
- Provide a local dashboard and JSON API for operator visibility.

## Non-Goals For MVP

- No auto-merge.
- No direct OpenAI API usage.
- No multi-tenant auth system.
- No distributed workers.
- No automatic destructive cleanup of workspaces unless explicitly configured.
- No requirement to use `codex app-server` in the first implementation.
- No requirement for Codex itself to write Jira comments or transition Jira issues.

## Recommended Tech Stack

- Python 3.11+
- `httpx` for Jira REST calls
- `pydantic` for typed config and domain models
- `PyYAML` for `WORKFLOW.md` front matter
- `jinja2` with `StrictUndefined` for prompt rendering
- `sqlite3` from the standard library for local run persistence
- `fastapi` and `uvicorn` for dashboard/API
- `pytest` for tests

Optional but useful:

- `rich` for local terminal status output
- `watchfiles` for workflow reloads

## Proposed Project Layout

Create this Python implementation under a new directory so it does not disturb the Elixir prototype:

```text
symphony/
  python/
    pyproject.toml
    README.md
    WORKFLOW.example.md
    symphony_jira/
      __init__.py
      __main__.py
      cli.py
      config.py
      workflow.py
      models.py
      jira.py
      orchestrator.py
      workspace.py
      codex_runner.py
      store.py
      dashboard.py
      logging.py
    tests/
      test_workflow.py
      test_config.py
      test_jira_models.py
      test_workspace.py
      test_codex_runner.py
```

## Command Line Interface

Expose a command named `symphony-jira`.

Required commands:

```bash
symphony-jira run ./WORKFLOW.md
symphony-jira dashboard ./WORKFLOW.md --port 3333
symphony-jira once ./WORKFLOW.md --issue ICPM-73100
symphony-jira validate ./WORKFLOW.md
```

Behavior:

- `run`: starts polling, dispatching, and dashboard API if configured.
- `dashboard`: starts only the dashboard/API against the local SQLite store.
- `once`: runs one Jira issue immediately, regardless of polling cadence, but still validates active/required labels unless `--force` is added.
- `validate`: parses and validates the workflow, checks Codex CLI availability, checks Jira credentials if configured.

## WORKFLOW.md Contract

Use Markdown with optional YAML front matter. Front matter is runtime config. Markdown body is the Codex prompt template.

Example:

```md
---
tracker:
  kind: jira
  base_url: "https://gbujira.oraclecorp.com"
  auth:
    mode: pat
    token_env: JIRA_TOKEN
    email_env: JIRA_EMAIL
  jql: 'project = ICPM AND labels = codex-ready AND status in ("To Do", "In Progress", "Rework")'
  required_labels: ["codex-ready"]
  active_statuses: ["To Do", "In Progress", "Rework"]
  terminal_statuses: ["Done", "Closed", "Cancelled", "Canceled", "Duplicate"]
  handoff_status: "Code Review"
  comment_on_start: true
  comment_on_finish: true

polling:
  interval_seconds: 60

workspace:
  root: "~/codex-workspaces"
  strategy: git_worktree
  source_repo: "/home/adkuppa/foyr2"
  branch_prefix: "codex"

hooks:
  after_create: |
    git status --short
  before_run: |
    git status --short
  verify: |
    npm test

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

Repository rules:
- Implement the smallest correct change for this issue.
- Keep unrelated refactors out of scope.
- Run the configured verification command.
- Leave a concise final report with files changed, verification, and residual risk.
```

### Workflow Config Rules

- Unknown top-level keys should be ignored.
- Invalid known values should fail `validate` and block dispatch.
- Environment variables should be resolved only when explicitly referenced by fields like `token_env`, `email_env`, or path values containing `$VAR`.
- `workspace.root` supports `~` expansion.
- Relative paths resolve relative to the workflow file directory.
- The prompt template must use strict undefined variables. Unknown template variables should fail the run attempt.

## Domain Models

### Issue

Normalized Jira issue model:

```python
class Issue(BaseModel):
    id: str
    identifier: str
    title: str
    description: str | None
    status: str
    priority: str | None
    issue_type: str | None
    assignee: str | None
    reporter: str | None
    labels: list[str]
    url: str
    created_at: datetime | None
    updated_at: datetime | None
    comments: list[IssueComment] = []
    blocked_by: list[IssueBlocker] = []
```

### Run

Persist every attempt:

```python
class RunRecord(BaseModel):
    id: str
    issue_id: str
    issue_identifier: str
    workspace_path: str
    status: Literal["queued", "running", "blocked", "failed", "completed", "cancelled"]
    attempt: int
    started_at: datetime
    finished_at: datetime | None
    final_message: str | None
    error: str | None
    branch_name: str | None
    verification_status: str | None
    verification_output_path: str | None
```

### Codex Event

Store raw JSONL events plus normalized fields:

```python
class CodexEvent(BaseModel):
    run_id: str
    sequence: int
    event_type: str
    raw_json: dict
    created_at: datetime
```

## Jira Adapter

Implement Jira REST support in `jira.py`.

Required methods:

```python
class JiraClient:
    async def search_issues(self, jql: str, limit: int) -> list[Issue]: ...
    async def get_issue(self, key: str, include_comments: bool = True) -> Issue: ...
    async def add_comment(self, key: str, body: str) -> None: ...
    async def transition_issue(self, key: str, target_status: str) -> None: ...
```

Implementation notes:

- Use `/rest/api/2/search` or `/rest/api/latest/search`.
- Use `/rest/api/2/issue/{key}` for issue details.
- Use `/rest/api/2/issue/{key}/comment` for comments.
- For transitions, call `/rest/api/2/issue/{key}/transitions`, find a transition whose target status matches the configured `handoff_status`, then POST that transition.
- If transition lookup fails, do not fail the whole run. Log it and include it in the final Jira comment.
- Normalize labels to lowercase for matching.
- Keep original label values in raw issue payload if useful for debugging.
- Support Personal Access Token bearer auth first:

```http
Authorization: Bearer $JIRA_TOKEN
```

- Basic auth with email/token can be supported as a fallback, but PAT bearer auth is enough for MVP if it works in the target Jira instance.

## Workspace Manager

Workspace path:

```text
{workspace.root}/{safe_issue_identifier}
```

Sanitization:

- Replace any character outside `[A-Za-z0-9._-]` with `_`.

Supported strategies:

### `git_worktree`

- `workspace.source_repo` points to an existing local git repository.
- Create a branch named `{branch_prefix}/{issue_identifier}`.
- Create a worktree at the issue workspace path.
- If the worktree already exists, reuse it.
- Never delete a dirty existing workspace automatically.

Expected command shape:

```bash
git -C "$SOURCE_REPO" worktree add -B "codex/ICPM-73100" "$WORKSPACE" HEAD
```

### `clone`

- `workspace.source_repo` may be a remote URL or local path.
- Clone into the workspace when it does not exist.
- Create/switch to the branch after clone.

### `hook_only`

- Only create the directory and run `hooks.after_create`.
- Useful when the workflow fully controls workspace setup.

## Hooks

Supported hooks:

- `after_create`: run once after a new workspace is created.
- `before_run`: run before Codex starts.
- `verify`: run after Codex finishes successfully.
- `after_run`: run after Codex finishes, regardless of status.

Hook behavior:

- Hooks run with `cwd` set to the issue workspace.
- Capture stdout/stderr to `.symphony/hooks/{hook_name}.log`.
- `after_create` and `before_run` failures should fail the attempt.
- `verify` failure should mark verification failed but still produce a Jira comment.
- `after_run` failure should be logged but should not mask the Codex result.

## Codex Runner

The MVP runner uses `codex exec --json`.

Default command:

```bash
codex exec --json --sandbox workspace-write "<rendered prompt>"
```

Run process details:

- Use `asyncio.create_subprocess_exec`.
- Set `cwd` to the issue workspace.
- Do not set `OPENAI_API_KEY` or `CODEX_API_KEY`.
- Stream stdout line by line.
- Treat stdout as JSONL events.
- Store every parseable event in SQLite.
- Store unparseable lines as raw log lines.
- Capture stderr to `.symphony/codex-stderr.log`.
- Enforce `agent.timeout_seconds`.
- On timeout, terminate the process, then kill it if it does not exit quickly.

Final message extraction:

- Prefer the last JSONL event of type `item.completed` whose item is an agent message.
- Also support `turn.completed` final status where available.
- Write final message to `.symphony/codex-final.md`.

Blocked handling:

- If Codex exits nonzero due to approval, sandbox, MCP elicitation, or user input required, mark the run `blocked`.
- Preserve the workspace.
- Comment on Jira only if `tracker.comment_on_finish` is true, with clear blocked reason.

Security:

- Use `workspace-write` by default.
- Do not use `danger-full-access` unless explicitly configured.
- Do not run with network unless the user's Codex config or sandbox policy allows it.
- Do not expose Jira tokens or Codex auth files in logs.

## Orchestrator

Runtime state:

- `claimed`: issues currently running or queued for retry.
- `running`: active subprocess tasks.
- `retry_queue`: issue id to next retry time and attempt count.
- `completed`: best-effort local set, not a permanent dispatch gate.

Dispatch loop:

1. Load and validate current workflow.
2. Query Jira with configured JQL.
3. Normalize issues.
4. Filter by:
   - active status
   - required labels
   - not already claimed
   - not blocked by unresolved blockers, if blockers are implemented
5. Sort by priority, then updated time.
6. Start up to `agent.max_concurrent_agents` workers.
7. Reconcile running issues on each poll:
   - If a running issue is no longer active, cancel its worker.
   - If a terminal issue has a workspace, leave it alone by default and mark inactive.

Worker lifecycle:

1. Mark run queued/running in SQLite.
2. Optionally comment "Codex started" on Jira.
3. Prepare workspace.
4. Run `after_create` if new.
5. Render prompt.
6. Run `before_run`.
7. Start Codex.
8. Store Codex events.
9. Run `verify` if Codex completed.
10. Run `after_run`.
11. Write final Jira comment.
12. Optionally transition Jira to `handoff_status` if configured.
13. Release claim.

Retry behavior:

- Retry transient failures up to `agent.max_retries`.
- Use exponential backoff capped by `agent.max_retry_backoff_seconds`.
- Do not retry template errors, invalid workflow, missing Codex CLI, missing workspace source repo, or Jira auth failures.

## SQLite Store

Create local DB at:

```text
{workflow_dir}/.symphony/symphony.sqlite3
```

Tables:

- `runs`
- `codex_events`
- `logs`
- `jira_actions`

The implementation can create schemas directly in Python. No migration framework is required for MVP.

Minimum run fields:

- id
- issue_id
- issue_identifier
- status
- attempt
- workspace_path
- branch_name
- started_at
- finished_at
- final_message
- error
- verification_status
- verification_output_path

## Dashboard And API

Use FastAPI.

Required JSON endpoints:

```text
GET /api/v1/state
GET /api/v1/runs
GET /api/v1/runs/{run_id}
GET /api/v1/issues/{issue_key}
POST /api/v1/refresh
```

Minimum dashboard page:

```text
GET /
```

Dashboard should show:

- Current workflow file path
- Jira JQL
- Poll interval
- Running issues
- Queued/retry issues
- Blocked issues
- Recent completed/failed runs
- Workspace path per issue
- Final Codex message
- Verification status
- Link to Jira issue

No authentication is required for MVP if binding to `127.0.0.1` only.

Default bind:

```text
127.0.0.1:3333
```

## Jira Comment Format

Start comment:

```md
Codex run started for ICPM-73100.

Workspace: `/home/adkuppa/codex-workspaces/ICPM-73100`
Branch: `codex/ICPM-73100`
```

Finish comment:

```md
Codex run completed for ICPM-73100.

Status: completed
Branch: `codex/ICPM-73100`
Workspace: `/home/adkuppa/codex-workspaces/ICPM-73100`

Verification:
- `verify`: passed

Summary:
<final Codex message>

Notes:
- Review the branch before merging.
```

Failure comment:

```md
Codex run failed for ICPM-73100.

Status: failed
Workspace: `/home/adkuppa/codex-workspaces/ICPM-73100`

Error:
<short error>

Logs are available in the local Symphony dashboard.
```

Blocked comment:

```md
Codex run is blocked for ICPM-73100.

Reason:
<approval/user-input/sandbox/tool issue>

Workspace: `/home/adkuppa/codex-workspaces/ICPM-73100`
```

## Preflight Checks

`symphony-jira validate` must check:

- Workflow file exists.
- YAML front matter parses to a mapping.
- `tracker.kind == "jira"`.
- `tracker.base_url` is configured.
- Jira token environment variable is present.
- `tracker.jql` is non-empty.
- Workspace root can be created or already exists.
- `workspace.source_repo` exists when using `git_worktree`.
- `codex` command is found on PATH.
- `codex --version` succeeds.

Optional but useful:

- Run a lightweight Codex smoke test only when `--codex-smoke-test` is passed:

```bash
codex exec --json --sandbox read-only "Reply with OK."
```

## Testing Requirements

Unit tests:

- Parse `WORKFLOW.md` with and without front matter.
- Reject non-map front matter.
- Strict prompt rendering fails on unknown variables.
- Jira issue payload normalizes into `Issue`.
- Label matching is case-insensitive.
- Workspace key sanitization.
- Workspace manager does not delete dirty directories.
- Codex runner parses JSONL events and extracts final agent message.
- Orchestrator does not dispatch beyond configured concurrency.
- Retry policy caps exponential backoff.

Integration tests with fakes:

- Fake Jira server returns one eligible issue.
- Fake Codex executable emits JSONL and exits 0.
- Worker creates a run record, stores events, writes final comment to fake Jira.

Do not require real Jira or real Codex for normal test suite.

Real e2e test:

- Behind explicit env flag only, for example `SYMPHONY_JIRA_E2E=1`.
- Uses a real Jira issue key and real local Codex CLI.
- Must never run by default.

## Implementation Phases

### Phase 1: Local skeleton

- Create Python package under `python/`.
- Implement workflow parsing, typed config, CLI `validate`.
- Add tests.

### Phase 2: Jira read path

- Implement Jira client search/get issue/comment parsing.
- Implement `once --issue KEY --dry-run` to print normalized issue and rendered prompt.
- Add fake Jira tests.

### Phase 3: Workspace and Codex runner

- Implement workspace manager with `git_worktree`.
- Implement `codex exec --json` runner.
- Add fake Codex executable tests.

### Phase 4: Single issue execution

- Implement `symphony-jira once ./WORKFLOW.md --issue ICPM-73100`.
- Run workspace setup, Codex, verify hook, SQLite persistence, Jira comment.

### Phase 5: Polling orchestrator

- Implement `run` with JQL polling, concurrency, retries, and reconciliation.

### Phase 6: Dashboard

- Implement FastAPI JSON endpoints and a minimal HTML dashboard.

### Phase 7: Hardening

- Redact secrets in logs.
- Better blocked-state detection.
- Optional Jira transition to handoff status.
- Optional PR creation hook.
- Optional app-server runner.

## Acceptance Criteria

The implementation is acceptable when:

- `symphony-jira validate ./WORKFLOW.md` catches missing Jira credentials and missing Codex CLI.
- `symphony-jira once ./WORKFLOW.md --issue ICPM-73100 --dry-run` renders a prompt without running Codex.
- `symphony-jira once ./WORKFLOW.md --issue ICPM-73100` creates or reuses a workspace and runs local `codex exec --json`.
- The service never asks for or uses `OPENAI_API_KEY`.
- A completed run stores events and final message in SQLite.
- A completed run writes a Jira comment summarizing status, workspace, branch, verification, and final Codex report.
- `symphony-jira run ./WORKFLOW.md` polls configured JQL and respects `max_concurrent_agents`.
- `symphony-jira dashboard ./WORKFLOW.md --port 3333` serves the dashboard at `http://127.0.0.1:3333`.
- Unit and fake integration tests pass without real Jira or real Codex.

## Suggested Prompt For The Build Agent

Use this prompt in a fresh Codex session from the root of the cloned Symphony repository:

### Goal Mode Prompt

If Codex Goal Mode is available, prefer this command:

```text
/goal Implement the Python/Jira/Codex-CLI Symphony MVP described in docs/JIRA_CODEX_PYTHON_SPEC.md through Phase 4. Keep the upstream Elixir implementation untouched. Put all new implementation files under python/. Do not require OPENAI_API_KEY or CODEX_API_KEY; use the local codex CLI through subprocess execution. Keep Jira writes in the Python orchestrator. Add normal tests using fake Jira and fake Codex so the test suite does not require real Jira or real Codex. Finish by running the tests and reporting what remains for Phase 5+.
```

Goal completion criteria:

- `python/` contains a runnable Python package and CLI.
- `symphony-jira validate` works against a workflow file.
- `symphony-jira once --issue KEY --dry-run` renders a prompt without running Codex.
- The single-issue execution path can prepare a workspace, run a fake or local Codex runner, persist events, and create a Jira result comment through the Jira adapter.
- Tests for workflow parsing, config validation, Jira normalization, workspace handling, Codex JSONL parsing, and single-issue execution pass.
- The implementation does not require real Jira, real Codex, `OPENAI_API_KEY`, or `CODEX_API_KEY` for the normal test suite.

### Normal Prompt

If Goal Mode is unavailable, use this as a normal prompt:

```text
Implement the Python/Jira/Codex-CLI Symphony MVP described in docs/JIRA_CODEX_PYTHON_SPEC.md.

Important constraints:
- Do not require OPENAI_API_KEY or CODEX_API_KEY.
- Use the local codex CLI through subprocess execution.
- Keep Jira writes in the Python orchestrator.
- Put the implementation under python/.
- Add tests that use fake Jira and fake Codex; do not require real Jira or real Codex for the normal test suite.
- Keep the upstream Elixir implementation untouched.

Start with Phase 1 through Phase 4, then run the test suite and report remaining work for Phase 5+.
```
