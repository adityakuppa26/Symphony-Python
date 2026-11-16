# Symphony Jira

A Python development orchestrator that turns Jira requirements into reviewed code
using the local Codex CLI.

The workflow is **planning → human approval → implementation → focused tests →
code review → handoff**. Verification is advisory. Later human feedback resumes
the existing implementation with its saved plan, requirements, and review context.

## Run

From the repository root, start Symphony:

```sh
./run-symphony.sh
```

Start the dashboard in another terminal, then open <http://127.0.0.1:3333>:

```sh
./run-dashboard.sh
```

Both launchers use `python/.venv/bin/python` and `python/WORKFLOW.md` by default.
They run in the foreground; press Ctrl-C in each terminal to stop. You can invoke
them by absolute path from any directory. Run either with `--help` for usage.

If the virtual environment is not installed yet:

```sh
python3 -m venv python/.venv
python/.venv/bin/python -m pip install -e ./python
```

Jira credentials and Codex setup must also be configured as described in the guide.
To customize the dashboard port or use another workflow:

```sh
./run-dashboard.sh --port 3334
./run-symphony.sh /path/to/WORKFLOW.md
./run-dashboard.sh /path/to/WORKFLOW.md --port 3334
```

Use the same workflow for both processes to share run history and approvals.
Relative workflow paths resolve from your current directory. Set `SYMPHONY_PYTHON`
to an alternative Python interpreter with the package dependencies installed.
The existing `python -m symphony_jira` commands remain available.

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
