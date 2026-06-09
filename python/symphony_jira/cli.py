from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .config import resolve_configured_secret, validate_preflight
from .dashboard import create_app
from .jira import JiraClient
from .orchestrator import PollingOrchestrator, SingleIssueOrchestrator, assert_issue_eligible
from .store import Store
from .workflow import WorkflowError, load_workflow, render_prompt


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            return command_validate(args)
        if args.command == "once":
            return asyncio.run(command_once(args))
        if args.command == "run":
            return asyncio.run(command_run(args))
        if args.command == "dashboard":
            return command_dashboard(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="symphony-jira")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a WORKFLOW.md file")
    validate.add_argument("workflow")

    once = subparsers.add_parser("once", help="run one Jira issue")
    once.add_argument("workflow")
    once.add_argument("--issue")
    once.add_argument("--force", action="store_true")
    once.add_argument("--dry-run", action="store_true")

    run = subparsers.add_parser("run", help="start the Phase 5 polling loop")
    run.add_argument("workflow")
    run.add_argument("--poll-once", action="store_true", help=argparse.SUPPRESS)

    dashboard = subparsers.add_parser("dashboard", help="start the Phase 6 dashboard")
    dashboard.add_argument("workflow")
    dashboard.add_argument("--port", type=int, default=3333)
    dashboard.add_argument("--host", default="127.0.0.1")
    return parser


def command_validate(args: argparse.Namespace) -> int:
    workflow = load_workflow(args.workflow)
    issues = validate_preflight(Path(args.workflow), workflow.config)
    if issues:
        for issue in issues:
            print(f"{issue.code}: {issue.message}", file=sys.stderr)
        return 1
    print("Workflow valid.")
    return 0


async def command_once(args: argparse.Namespace) -> int:
    workflow = load_workflow(args.workflow)
    preflight = validate_preflight(
        workflow.path,
        workflow.config,
        check_jira_credentials=True,
        check_codex=not args.dry_run,
    )
    if preflight:
        for issue in preflight:
            print(f"{issue.code}: {issue.message}", file=sys.stderr)
        return 1

    store = Store(workflow.path.parent / ".symphony" / "symphony.sqlite3")
    async with JiraClient(workflow.config.tracker, environ=os.environ) as jira:
        if not args.issue:
            if not args.dry_run:
                print("once requires --issue unless --dry-run is set.", file=sys.stderr)
                return 2
            return await command_once_dry_run_from_jql(args, workflow, jira)

        orchestrator = SingleIssueOrchestrator(workflow, jira, store, secret_values=secret_values_for(workflow))
        result = await orchestrator.run_once(args.issue, force=args.force, dry_run=args.dry_run)

    if args.dry_run:
        print("Issue:")
        print(json.dumps(result.issue.model_dump(mode="json", exclude={"raw"}), indent=2, sort_keys=True))
        print("\nRendered prompt:")
        print(result.prompt)
        return 0

    assert result.run is not None
    print(f"Run {result.run.id} finished with status: {result.run.status}")
    if result.run.final_message:
        print("\nFinal Codex message:")
        print(result.run.final_message)
    if result.run.error:
        print("\nError:")
        print(result.run.error)
    return 0 if result.run.status == "completed" else 1


async def command_once_dry_run_from_jql(args: argparse.Namespace, workflow, jira) -> int:
    issues = await jira.search_issues(workflow.config.tracker.jql, limit=25)
    eligible = []
    skipped = []
    for issue in issues:
        if args.force:
            eligible.append(issue)
            continue
        try:
            assert_issue_eligible(issue, workflow.config)
            eligible.append(issue)
        except Exception as exc:
            skipped.append((issue, str(exc)))

    print(f"JQL matched {len(issues)} issue(s).")
    if skipped:
        print(f"Skipped {len(skipped)} ineligible issue(s).")
    if not eligible:
        print("No eligible issues matched the workflow JQL.")
        return 1

    print("Eligible issues:")
    for issue in eligible:
        print(f"- {issue.identifier} | {issue.status} | {issue.title}")

    if len(eligible) != 1:
        print("\nDry-run prompt rendering needs exactly one issue. Re-run with --issue <KEY>.")
        return 0

    issue = await jira.get_issue(eligible[0].identifier, include_comments=True)
    if not args.force:
        assert_issue_eligible(issue, workflow.config)
    prompt = render_prompt(workflow, issue)
    print("\nIssue:")
    print(json.dumps(issue.model_dump(mode="json", exclude={"raw"}), indent=2, sort_keys=True))
    print("\nRendered prompt:")
    print(prompt)
    return 0


async def command_run(args: argparse.Namespace) -> int:
    workflow = load_workflow(args.workflow)
    preflight = validate_preflight(workflow.path, workflow.config, check_jira_credentials=True, check_codex=True)
    if preflight:
        for issue in preflight:
            print(f"{issue.code}: {issue.message}", file=sys.stderr)
        return 1

    store = Store(workflow.path.parent / ".symphony" / "symphony.sqlite3")
    async with JiraClient(workflow.config.tracker, environ=os.environ) as jira:
        orchestrator = PollingOrchestrator(workflow, jira, store, secret_values=secret_values_for(workflow))
        if args.poll_once:
            await orchestrator.poll_once()
            await orchestrator.reap_finished()
            print(json.dumps(orchestrator.snapshot(), indent=2, sort_keys=True))
            return 0
        print(f"Polling Jira every {workflow.config.polling.interval_seconds}s. Press Ctrl-C to stop.")
        await orchestrator.run_forever()
    return 0


def command_dashboard(args: argparse.Namespace) -> int:
    workflow = load_workflow(args.workflow)
    store = Store(workflow.path.parent / ".symphony" / "symphony.sqlite3")
    app = create_app(workflow, store)
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is not installed. Install package dependencies first.") from exc
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def secret_values_for(workflow) -> list[str | None]:
    auth = workflow.config.tracker.auth
    values = [
        resolve_configured_secret(
            env_name=auth.token_env,
            config_file=auth.token_config_file,
            config_key=auth.token_config_key,
            environ=os.environ,
        )
    ]
    values.append(
        resolve_configured_secret(
            env_name=auth.email_env,
            config_file=auth.token_config_file,
            config_key=auth.email_config_key,
            environ=os.environ,
        )
    )
    return values


if __name__ == "__main__":
    raise SystemExit(main())
