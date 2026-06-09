# Symphony Jira Python MVP

It uses Jira REST credentials from `WORKFLOW.md`, prepares a per-issue local
workspace, runs the locally installed `codex` CLI with `codex exec --json`, stores
run data in SQLite, and posts Jira comments from the Python orchestrator.

It does not require `OPENAI_API_KEY` or `CODEX_API_KEY`. Codex authentication is
whatever the local `codex` CLI already uses.

## Commands

```bash
python3 -m symphony_jira validate ./WORKFLOW.md
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100 --dry-run
python3 -m symphony_jira once ./WORKFLOW.md --issue ICPM-73100
python3 -m symphony_jira run ./WORKFLOW.md
python3 -m symphony_jira dashboard ./WORKFLOW.md --port 3333
```

