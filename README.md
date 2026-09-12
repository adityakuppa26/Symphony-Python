# Symphony Jira

A Python development orchestrator that turns Jira requirements into reviewed code
using the local Codex CLI.

The workflow is **planning → human approval → implementation → focused tests →
code review → handoff**. Verification is advisory. Later human feedback resumes
the existing implementation with its saved plan, requirements, and review context.

## Run

The application lives in `python/`; existing command and configuration paths are
unchanged. From that directory, using its configured virtual environment:

```sh
cd python
.venv/bin/python -m symphony_jira validate ./WORKFLOW.md
.venv/bin/python -m symphony_jira run ./WORKFLOW.md
```

Run the dashboard in another terminal:

```sh
cd python
.venv/bin/python -m symphony_jira dashboard ./WORKFLOW.md --port 3333
```

See the [Python workflow guide](python/README.md) for setup, Jira configuration,
approvals, feedback, and host test execution. The active configuration and agent
instructions are in [WORKFLOW.md](python/WORKFLOW.md).

## Repository layout

| Path | Purpose |
| --- | --- |
| `python/symphony_jira/` | CLI, Jira ingestion, orchestration, planning, saved context, dashboard, and verification |
| `python/symphony_jira/handlers/development.py` | Development planning, implementation, and review policy |
| `python/scripts/` | Host-side test runtime and container adapters |
| `python/tests/` | Python regression tests and browser-client behavior checks |
| `python/pyproject.toml`, `python/uv.lock` | Python package metadata and dependency lock |
| `python/WORKFLOW.md` | Development workflow configuration and prompts |

Local run data, approvals, artifacts, and caches live in `python/.symphony/` and
are excluded from version control. Preserve this directory when continuing
existing cases.

## Check changes

```sh
cd python
.venv/bin/python -m pytest -q
```

The project retains its [Apache 2.0 license](LICENSE) and [copyright notice](NOTICE).
