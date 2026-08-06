from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateError

from .config import HooksConfig, WorkspaceConfig


class WorkspaceError(Exception):
    """Raised when workspace preparation or hooks fail."""


@dataclass(frozen=True)
class WorkspaceInfo:
    issue_identifier: str
    path: Path
    created: bool
    branch_name: str | None


@dataclass(frozen=True)
class HookResult:
    name: str
    returncode: int
    log_path: Path
    output: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


def sanitize_workspace_key(identifier: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", identifier)
    return safe or "issue"


class WorkspaceManager:
    def __init__(self, config: WorkspaceConfig, hooks: HooksConfig) -> None:
        self.config = config
        self.hooks = hooks
        self.root = config.root.resolve()

    def workspace_path_for(self, issue_identifier: str) -> Path:
        path = (self.root / sanitize_workspace_key(issue_identifier)).resolve()
        self._assert_inside_root(path)
        return path

    def branch_name_for(self, issue_identifier: str) -> str | None:
        if self.config.strategy in {"git_worktree", "clone"}:
            return f"{self.config.branch_prefix}/{issue_identifier}"
        return None

    async def prepare(self, issue_identifier: str, hook_context: dict[str, Any] | None = None) -> WorkspaceInfo:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.workspace_path_for(issue_identifier)
        branch_name = self.branch_name_for(issue_identifier)

        if path.exists():
            if not path.is_dir():
                raise WorkspaceError(f"Workspace path exists and is not a directory: {path}")
            return WorkspaceInfo(issue_identifier=issue_identifier, path=path, created=False, branch_name=branch_name)

        if self.config.strategy == "hook_only":
            path.mkdir(parents=True, exist_ok=False)
        elif self.config.strategy == "git_worktree":
            await self._prepare_git_worktree(path, branch_name)
        elif self.config.strategy == "clone":
            await self._prepare_clone(path, branch_name)
        else:
            raise WorkspaceError(f"Unsupported workspace strategy: {self.config.strategy}")

        info = WorkspaceInfo(issue_identifier=issue_identifier, path=path, created=True, branch_name=branch_name)
        if self.hooks.after_create:
            context = self._hook_context(hook_context, info)
            result = await self.run_hook("after_create", self.hooks.after_create, path, hook_context=context)
            if not result.succeeded:
                raise WorkspaceError(f"after_create hook failed; see {result.log_path}")
        return info

    async def run_hook(
        self,
        name: str,
        script: str,
        workspace_path: Path,
        hook_context: dict[str, Any] | None = None,
    ) -> HookResult:
        hook_dir = workspace_path / ".symphony" / "hooks"
        hook_dir.mkdir(parents=True, exist_ok=True)
        log_path = hook_dir / f"{name}.log"
        rendered_script = render_hook_script(script, hook_context) if hook_context else script
        return await asyncio.to_thread(
            self._run_hook_blocking,
            name,
            rendered_script,
            workspace_path,
            log_path,
        )

    def _hook_context(self, hook_context: dict[str, Any] | None, workspace: WorkspaceInfo) -> dict[str, Any]:
        context = dict(hook_context or {})
        context.setdefault("workspace", workspace)
        context.setdefault("workspace_path", str(workspace.path))
        context.setdefault("branch_name", workspace.branch_name)
        return context

    def _run_hook_blocking(self, name: str, script: str, workspace_path: Path, log_path: Path) -> HookResult:
        try:
            completed = subprocess.run(
                ["bash", "-lc", script],
                cwd=str(workspace_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.hooks.timeout_seconds,
                check=False,
            )
            output = completed.stdout or ""
            log_path.write_text(output, encoding="utf-8")
            return HookResult(name=name, returncode=completed.returncode, log_path=log_path, output=output)
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            log_path.write_text(output + "\n[TIMEOUT]\n", encoding="utf-8")
            return HookResult(name=name, returncode=124, log_path=log_path, output=output)

    async def _prepare_git_worktree(self, path: Path, branch_name: str | None) -> None:
        source_repo = self.config.source_repo
        if not source_repo:
            raise WorkspaceError("workspace.source_repo is required for git_worktree")
        source_path = Path(source_repo)
        if not source_path.exists():
            raise WorkspaceError(f"workspace.source_repo does not exist: {source_repo}")
        if not branch_name:
            raise WorkspaceError("branch name could not be computed")
        await self._run_git(
            ["git", "-C", str(source_path), "worktree", "add", "-B", branch_name, str(path), "HEAD"],
            cwd=source_path,
            log_path=self.root / f"{sanitize_workspace_key(branch_name)}.git-worktree.log",
        )

    async def _prepare_clone(self, path: Path, branch_name: str | None) -> None:
        source_repo = self.config.source_repo
        if not source_repo:
            raise WorkspaceError("workspace.source_repo is required for clone")
        await self._run_git(
            ["git", "clone", source_repo, str(path)],
            cwd=self.root,
            log_path=self.root / f"{sanitize_workspace_key(path.name)}.git-clone.log",
        )
        if branch_name:
            await self._run_git(
                ["git", "-C", str(path), "checkout", "-B", branch_name],
                cwd=path,
                log_path=self.root / f"{sanitize_workspace_key(branch_name)}.git-checkout.log",
            )

    async def _run_git(self, args: list[str], cwd: Path, log_path: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()
        output = stdout.decode(errors="replace")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        if process.returncode != 0:
            raise WorkspaceError(f"Git command failed with exit {process.returncode}; see {log_path}")

    def _assert_inside_root(self, path: Path) -> None:
        root = str(self.root)
        candidate = str(path)
        if os.path.commonpath([root, candidate]) != root:
            raise WorkspaceError(f"Workspace path escapes workspace.root: {path}")


def render_hook_script(script: str, context: dict[str, Any]) -> str:
    env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
    try:
        return env.from_string(script).render(**context)
    except TemplateError as exc:
        raise WorkspaceError(f"Hook template rendering failed: {exc}") from exc
