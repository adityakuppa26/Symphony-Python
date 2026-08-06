from __future__ import annotations

import asyncio
import fcntl
import heapq
import json
import os
import re
import signal
import stat
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    BinaryIO,
    Callable,
    Iterable,
    Iterator,
    Literal,
    Mapping,
    Protocol,
    Sequence,
)

from .config import (
    RuntimeConfig,
    RuntimeRepositoryConfig,
    RuntimeVerificationProfileConfig,
)
from .environment import (
    DEFAULT_JIRA_ENVIRONMENT_EXCLUDE_PATTERNS,
    filtered_subprocess_environment,
)


VerificationStatus = Literal["passed", "test_failed", "environment_blocked"]
PreviewStatus = Literal["started", "stopped", "environment_blocked"]
ShutdownStatus = Literal["stopped", "environment_blocked"]
_RENDERED_CONFIG_OMITTED = "[rendered Compose configuration omitted]"
_SENSITIVE_CONFIG_KEY = (
    r"(?:pass(?:word|wd)?|token|secret|credential|auth|api[_-]?key|"
    r"private[_-]?key|dsn|database[_-]?url|connection[_-]?string)"
)
_SENSITIVE_JSON_VALUE = re.compile(
    rf'("(?:(?:[^"\\]|\\.)*{_SENSITIVE_CONFIG_KEY}(?:[^"\\]|\\.)*)"'
    r'\s*:\s*)"(?:[^"\\]|\\.)*"',
    re.IGNORECASE,
)
_SENSITIVE_ENV_ASSIGNMENT = re.compile(
    rf"(?P<prefix>\b[A-Za-z_][A-Za-z0-9_]*{_SENSITIVE_CONFIG_KEY}"
    r"[A-Za-z0-9_]*\s*=\s*)"
    r'(?P<value>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|'
    r"[^\s,;\]}\"\']+)",
    re.IGNORECASE,
)
_CREDENTIAL_URL = re.compile(
    r"\b(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@",
    re.IGNORECASE,
)
_DNSNAME_PERMISSION_HINT = (
    "Host Podman CNI dnsname could not signal dnsmasq while configuring "
    "container DNS. Repository tests did not run. This host-policy failure is "
    "not safe to retry automatically. On an AppArmor host, allow only SIGHUP "
    "from peer podman in /etc/apparmor.d/local/usr.sbin.dnsmasq and reload "
    "/etc/apparmor.d/usr.sbin.dnsmasq; otherwise repair or migrate the rootless "
    "Podman network before resuming."
)
_ROOTLESS_RUNTIME_PERMISSION_HINT = (
    "Host rootless Podman cannot write its runtime directory. Repository tests "
    "did not run. This environment failure is not safe to retry automatically; "
    "run Symphony with a writable XDG_RUNTIME_DIR outside a read-only sandbox, "
    "then resume."
)
_OPEN_OUTPUT_STREAM_MARKER = (
    "[output capture stopped because stdout remained open after the runtime "
    "command ended]"
)
_OUTPUT_TRUNCATION_MARKER = b"\n[output truncated]\n[output tail follows]\n"
_EARLIER_LOG_TRUNCATION_MARKER = b"[earlier runtime log entries truncated]\n"
_LOG_ENTRY_TRUNCATION_MARKER = (
    b"\n[retained runtime log entry truncated; prefix and suffix preserved]\n"
)


class RuntimeError(Exception):
    """Base error for an invalid or unavailable local runtime."""


class RuntimeConfigurationError(RuntimeError):
    """Raised when runtime configuration or workspace bindings are unsafe."""


class RuntimeCommandError(RuntimeError):
    """Raised when a runtime process cannot be started."""

    def __init__(self, message: str, argv: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.argv = tuple(argv)


class RuntimeTimeoutError(RuntimeError):
    """Raised when the shared runtime lane cannot be acquired."""


class RuntimeArtifactError(OSError):
    """Raised when runtime evidence cannot be written without following links."""


class _ReadableStream(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


class _Process(Protocol):
    pid: int
    returncode: int | None
    stdout: _ReadableStream | None

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[_Process]]


@dataclass
class _OutputCapture:
    head: bytearray
    tail: bytearray
    total_bytes: int = 0


@dataclass(frozen=True)
class RuntimeCommandResult:
    argv: tuple[str, ...]
    returncode: int | None
    output: str
    timed_out: bool
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True)
class RuntimeVerificationResult:
    repository: str
    profile: str
    status: VerificationStatus
    argv: tuple[str, ...]
    repository_path: Path | None
    started_at: datetime
    finished_at: datetime
    returncode: int | None
    output: str
    log_path: Path | None
    message: str


@dataclass(frozen=True)
class RuntimePreviewResult:
    repository: str
    status: PreviewStatus
    argv: tuple[str, ...]
    repository_path: Path | None
    started_at: datetime
    finished_at: datetime
    returncode: int | None
    output: str
    log_path: Path | None
    message: str


@dataclass(frozen=True)
class RuntimeShutdownResult:
    repositories: tuple[str, ...]
    services: tuple[str, ...]
    status: ShutdownStatus
    argv: tuple[str, ...]
    started_at: datetime
    finished_at: datetime
    returncode: int | None
    output: str
    log_path: Path | None
    message: str


class RuntimeManager:
    """Runs one serialized Podman Compose verification/preview/shutdown lane."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        environ: Mapping[str, str] | None = None,
        excluded_environment_names: Iterable[str | None] = (),
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self.config = config
        source_environment = os.environ if environ is None else environ
        self.environ = filtered_subprocess_environment(
            source_environment,
            excluded_names=excluded_environment_names,
            excluded_patterns=DEFAULT_JIRA_ENVIRONMENT_EXCLUDE_PATTERNS,
        )
        self._process_factory: ProcessFactory = (
            process_factory or asyncio.create_subprocess_exec
        )
        self._native_process_factory = process_factory is None

    async def verify(
        self,
        workspace_root: Path,
        repository: str,
        *,
        target_args: Sequence[str] = (),
        source_repositories: Sequence[str] = (),
    ) -> RuntimeVerificationResult:
        """Run a fixed verification profile against workspace bind mounts."""

        return await self._verify(
            workspace_root,
            repository,
            target_args=target_args,
            source_repositories=source_repositories,
            acquire_lock=True,
        )

    async def verify_many(
        self,
        workspace_root: Path,
        repositories: Sequence[str],
        *,
        target_args_by_repository: Mapping[str, Sequence[str]] | None = None,
        source_repositories: Sequence[str] = (),
    ) -> tuple[RuntimeVerificationResult, ...]:
        """Verify affected repositories atomically under one machine-wide lock."""

        if isinstance(repositories, (str, bytes)) or isinstance(
            source_repositories, (str, bytes)
        ):
            raise RuntimeConfigurationError(
                "repositories must be sequences of configured repository names"
            )
        repository_names = list(dict.fromkeys(repositories))
        if not repository_names:
            return ()
        all_sources = list(
            dict.fromkeys([*repository_names, *source_repositories])
        )
        requested_args = target_args_by_repository or {}
        unknown_args = set(requested_args).difference(repository_names)
        if unknown_args:
            raise RuntimeConfigurationError(
                "target arguments supplied for repositories not being verified: "
                + ", ".join(sorted(unknown_args))
            )
        async with self._runtime_lock():
            results: list[RuntimeVerificationResult] = []
            for repository in repository_names:
                result = await self._verify(
                    workspace_root,
                    repository,
                    target_args=requested_args.get(repository, ()),
                    source_repositories=all_sources,
                    acquire_lock=False,
                )
                results.append(result)
                # A host/configuration blocker prevents trustworthy execution
                # for every repository sharing this Compose lane. Do not make
                # another mutating Compose call that is expected to fail for
                # the same reason. Ordinary test failures still run the rest.
                if result.status == "environment_blocked":
                    break
            return tuple(results)

    async def _verify(
        self,
        workspace_root: Path,
        repository: str,
        *,
        target_args: Sequence[str],
        source_repositories: Sequence[str],
        acquire_lock: bool,
    ) -> RuntimeVerificationResult:
        started_at = _utcnow()
        profile_name = ""
        repository_path: Path | None = None
        log_path: Path | None = None
        last_command: RuntimeCommandResult | None = None
        try:
            self._assert_enabled()
            binding = self._binding(repository)
            profile_name = binding.verification_profile
            profile = self.config.verification_profiles[profile_name]
            selected_args = self._verification_args(profile, target_args)
            root, sources = self._resolve_sources(
                workspace_root, repository, source_repositories
            )
            repository_path = sources[repository]
            log_path = self._prepare_log(root, repository, "verify")
            environment = self._compose_environment(sources)

            async with self._optional_runtime_lock(acquire_lock):
                self._validate_runtime_paths()
                last_command, compose_config = await self._rendered_config(
                    environment, log_path
                )
                if last_command.timed_out or last_command.returncode != 0:
                    message = runtime_environment_blocker_message(
                        last_command.output
                    ) or "Podman Compose configuration could not be rendered"
                    return self._verification_result(
                        repository=repository,
                        profile=profile_name,
                        status="environment_blocked",
                        repository_path=repository_path,
                        started_at=started_at,
                        log_path=log_path,
                        command=last_command,
                        message=message,
                    )
                if compose_config is None:
                    raise RuntimeConfigurationError(
                        "Podman Compose configuration was not available"
                    )
                for source_name, source_path in sources.items():
                    self._assert_mount(
                        compose_config,
                        self._binding(source_name),
                        source_path,
                    )

                startup_services = list(
                    dict.fromkeys(
                        [*binding.force_recreate_dependencies, binding.service]
                    )
                )
                last_command = await self._execute_logged(
                    self._compose_prefix()
                    + [
                        "up",
                        "-d",
                        "--wait",
                        "--force-recreate",
                        *startup_services,
                    ],
                    environment,
                    profile.timeout_seconds,
                    log_path,
                )
                if last_command.timed_out or last_command.returncode != 0:
                    message = runtime_environment_blocker_message(
                        last_command.output
                    ) or "Runtime service failed to start"
                    return self._verification_result(
                        repository=repository,
                        profile=profile_name,
                        status="environment_blocked",
                        repository_path=repository_path,
                        started_at=started_at,
                        log_path=log_path,
                        command=last_command,
                        message=message,
                    )

                test_argv = self._compose_prefix() + [
                    "exec",
                    "-T",
                ]
                for name, value in profile.environment.items():
                    test_argv.extend(["-e", f"{name}={value}"])
                if binding.container_workdir:
                    test_argv.extend(["--workdir", binding.container_workdir])
                test_argv.extend([binding.service, *selected_args])
                last_command = await self._execute_logged(
                    test_argv,
                    environment,
                    profile.timeout_seconds,
                    log_path,
                )
                environment_blocker = runtime_environment_blocker_message(
                    last_command.output
                )
                if last_command.timed_out:
                    status: VerificationStatus = "test_failed"
                elif last_command.returncode == 0:
                    status = "passed"
                elif environment_blocker is not None or last_command.returncode in {
                    125,
                    126,
                    127,
                }:
                    status = "environment_blocked"
                else:
                    status = "test_failed"
                if status == "passed":
                    message = "Runtime verification passed"
                elif last_command.timed_out:
                    message = "Runtime verification timed out"
                elif environment_blocker is not None:
                    message = environment_blocker
                elif status == "environment_blocked":
                    message = (
                        "Runtime verification environment could not execute "
                        "the configured command"
                    )
                else:
                    message = "Runtime verification command failed"
                return self._verification_result(
                    repository=repository,
                    profile=profile_name,
                    status=status,
                    repository_path=repository_path,
                    started_at=started_at,
                    log_path=log_path,
                    command=last_command,
                    message=message,
                )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            command = last_command or _empty_command()
            if isinstance(exc, RuntimeCommandError) and exc.argv:
                command = RuntimeCommandResult(
                    argv=sanitize_argv(exc.argv),
                    returncode=None,
                    output=str(exc),
                    timed_out=False,
                    started_at=started_at,
                    finished_at=_utcnow(),
            )
            if log_path is not None:
                self._append_log_best_effort(log_path, str(exc))
            return self._verification_result(
                repository=repository,
                profile=profile_name,
                status="environment_blocked",
                repository_path=repository_path,
                started_at=started_at,
                log_path=log_path,
                command=command,
                message=str(exc),
            )

    async def start_preview(
        self,
        workspace_root: Path,
        repository: str,
        *,
        source_repositories: Sequence[str] = (),
        start_dependencies: bool = True,
    ) -> RuntimePreviewResult:
        """Force-recreate a service with workspace mounts for manual inspection."""

        return await self._change_preview(
            workspace_root,
            repository,
            source_repositories=source_repositories,
            start_dependencies=start_dependencies,
            stop=False,
        )

    async def stop_preview(
        self,
        workspace_root: Path,
        repository: str,
        *,
        source_repositories: Sequence[str] = (),
    ) -> RuntimePreviewResult:
        """Stop the configured preview service while holding the runtime lane."""

        return await self._change_preview(
            workspace_root,
            repository,
            source_repositories=source_repositories,
            start_dependencies=False,
            stop=True,
        )

    async def shutdown(
        self,
        workspace_root: Path,
        repositories: Sequence[str],
        *,
        source_repositories: Sequence[str] = (),
    ) -> RuntimeShutdownResult:
        """Stop selected services and their dependency closure without removal."""

        started_at = _utcnow()
        repository_names: tuple[str, ...] = ()
        service_order: tuple[str, ...] = ()
        log_path: Path | None = None
        last_command: RuntimeCommandResult | None = None
        try:
            self._assert_enabled()
            repository_names = self._validated_repository_names(
                repositories, "repositories"
            )
            source_names = self._validated_repository_names(
                source_repositories, "source_repositories"
            )
            root = self._resolve_workspace_root(workspace_root)
            log_path = self._prepare_log(root, "runtime", "shutdown")

            async with self._runtime_lock():
                if not repository_names:
                    last_command = _replace_command_output(
                        _empty_command(), "No runtime services selected"
                    )
                    self._append_log(log_path, last_command.output)
                    return self._shutdown_result(
                        repository_names,
                        service_order,
                        "stopped",
                        started_at,
                        log_path,
                        last_command,
                        "No runtime services required shutdown",
                    )

                selected_sources = list(
                    dict.fromkeys([*repository_names, *source_names])
                )
                _, sources = self._resolve_sources(
                    root,
                    selected_sources[0],
                    selected_sources[1:],
                )
                environment = self._compose_environment(sources)
                self._validate_runtime_paths()
                last_command, compose_config = await self._rendered_config(
                    environment, log_path
                )
                if last_command.timed_out or last_command.returncode != 0:
                    message = runtime_environment_blocker_message(
                        last_command.output
                    ) or "Podman Compose configuration could not be rendered"
                    return self._shutdown_result(
                        repository_names,
                        service_order,
                        "environment_blocked",
                        started_at,
                        log_path,
                        last_command,
                        message,
                    )
                if compose_config is None:
                    raise RuntimeConfigurationError(
                        "Podman Compose configuration was not available"
                    )
                for source_name, source_path in sources.items():
                    self._assert_mount(
                        compose_config,
                        self._binding(source_name),
                        source_path,
                    )

                service_order = self._shutdown_service_order(
                    compose_config, repository_names
                )
                stop_argv = self._compose_prefix() + [
                    "stop",
                    "--timeout",
                    str(self.config.shutdown_grace_seconds),
                    *service_order,
                ]
                last_command = await self._execute_logged(
                    stop_argv,
                    environment,
                    self.config.preview_timeout_seconds,
                    log_path,
                )
                if last_command.timed_out or last_command.returncode != 0:
                    message = runtime_environment_blocker_message(
                        last_command.output
                    )
                    if message is None:
                        message = (
                            "Runtime shutdown timed out"
                            if last_command.timed_out
                            else "Runtime shutdown command failed"
                        )
                    return self._shutdown_result(
                        repository_names,
                        service_order,
                        "environment_blocked",
                        started_at,
                        log_path,
                        last_command,
                        message,
                    )
                return self._shutdown_result(
                    repository_names,
                    service_order,
                    "stopped",
                    started_at,
                    log_path,
                    last_command,
                    "Runtime services stopped",
                )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            command = last_command or _empty_command()
            if isinstance(exc, RuntimeCommandError) and exc.argv:
                command = RuntimeCommandResult(
                    argv=sanitize_argv(exc.argv),
                    returncode=None,
                    output=str(exc),
                    timed_out=False,
                    started_at=started_at,
                    finished_at=_utcnow(),
            )
            if log_path is not None:
                self._append_log_best_effort(log_path, str(exc))
            return self._shutdown_result(
                repository_names,
                service_order,
                "environment_blocked",
                started_at,
                log_path,
                command,
                str(exc),
            )

    async def _change_preview(
        self,
        workspace_root: Path,
        repository: str,
        *,
        source_repositories: Sequence[str],
        start_dependencies: bool,
        stop: bool,
    ) -> RuntimePreviewResult:
        started_at = _utcnow()
        repository_path: Path | None = None
        log_path: Path | None = None
        last_command: RuntimeCommandResult | None = None
        try:
            self._assert_enabled()
            binding = self._binding(repository)
            root, sources = self._resolve_sources(
                workspace_root, repository, source_repositories
            )
            repository_path = sources[repository]
            log_path = self._prepare_log(root, repository, "preview")
            environment = self._compose_environment(sources)
            async with self._runtime_lock():
                self._validate_runtime_paths()
                if not stop:
                    last_command, compose_config = await self._rendered_config(
                        environment, log_path
                    )
                    if last_command.timed_out or last_command.returncode != 0:
                        raise RuntimeConfigurationError(
                            runtime_environment_blocker_message(
                                last_command.output
                            )
                            or "Podman Compose configuration could not be rendered"
                        )
                    if compose_config is None:
                        raise RuntimeConfigurationError(
                            "Podman Compose configuration was not available"
                        )
                    for source_name, source_path in sources.items():
                        self._assert_mount(
                            compose_config,
                            self._binding(source_name),
                            source_path,
                        )
                    if start_dependencies and binding.dependencies:
                        last_command = await self._start_dependencies(
                            binding,
                            environment,
                            self.config.preview_timeout_seconds,
                            log_path,
                        )
                        if last_command.timed_out or last_command.returncode != 0:
                            raise RuntimeConfigurationError(
                                runtime_environment_blocker_message(
                                    last_command.output
                                )
                                or "Runtime dependencies failed to start"
                            )
                    argv = self._compose_prefix() + [
                        "up",
                        "-d",
                        "--wait",
                        "--no-deps",
                        "--force-recreate",
                        binding.service,
                    ]
                    success_status: PreviewStatus = "started"
                    success_message = "Runtime preview started"
                else:
                    argv = self._compose_prefix() + ["stop", binding.service]
                    success_status = "stopped"
                    success_message = "Runtime preview stopped"

                last_command = await self._execute_logged(
                    argv,
                    environment,
                    self.config.preview_timeout_seconds,
                    log_path,
                )
                if last_command.timed_out or last_command.returncode != 0:
                    raise RuntimeConfigurationError(
                        runtime_environment_blocker_message(last_command.output)
                        or "Runtime preview command timed out or failed"
                    )
                return self._preview_result(
                    repository,
                    success_status,
                    repository_path,
                    started_at,
                    log_path,
                    last_command,
                    success_message,
                )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            command = last_command or _empty_command()
            if log_path is not None:
                self._append_log_best_effort(log_path, str(exc))
            return self._preview_result(
                repository,
                "environment_blocked",
                repository_path,
                started_at,
                log_path,
                command,
                str(exc),
            )

    def _assert_enabled(self) -> None:
        if not self.config.enabled:
            raise RuntimeConfigurationError("runtime is disabled")

    def _binding(self, repository: str) -> RuntimeRepositoryConfig:
        try:
            return self.config.repositories[repository]
        except KeyError as exc:
            raise RuntimeConfigurationError(
                f"runtime repository is not configured: {repository}"
            ) from exc

    def _validated_repository_names(
        self, repositories: Sequence[str], label: str
    ) -> tuple[str, ...]:
        if isinstance(repositories, (str, bytes)):
            raise RuntimeConfigurationError(
                f"{label} must be a sequence of repository names"
            )
        names = list(repositories)
        if any(
            not isinstance(name, str) or not name.strip() or "\x00" in name
            for name in names
        ):
            raise RuntimeConfigurationError(
                f"{label} must contain non-empty repository names with no NUL"
            )
        return tuple(dict.fromkeys(names))

    def _verification_args(
        self,
        profile: RuntimeVerificationProfileConfig,
        target_args: Sequence[str],
    ) -> list[str]:
        if isinstance(target_args, (str, bytes)):
            raise RuntimeConfigurationError(
                "runtime target_args must be a sequence of individual arguments"
            )
        requested = list(target_args)
        if any(
            not isinstance(argument, str)
            or not argument.strip()
            or "\x00" in argument
            for argument in requested
        ):
            raise RuntimeConfigurationError(
                "runtime target_args must be non-empty strings containing no NUL"
            )
        return [*profile.argv, *(requested or profile.default_args)]

    async def _start_dependencies(
        self,
        binding: RuntimeRepositoryConfig,
        environment: Mapping[str, str],
        timeout_seconds: int,
        log_path: Path,
    ) -> RuntimeCommandResult:
        recreate = set(binding.force_recreate_dependencies)
        reusable_dependencies = [
            dependency
            for dependency in binding.dependencies
            if dependency not in recreate
        ]
        recreated_dependencies = [
            dependency
            for dependency in binding.dependencies
            if dependency in recreate
        ]
        commands: list[list[str]] = []
        if reusable_dependencies:
            commands.append(
                self._compose_prefix()
                + ["up", "-d", "--wait", *reusable_dependencies]
            )
        if recreated_dependencies:
            commands.append(
                self._compose_prefix()
                + [
                    "up",
                    "-d",
                    "--wait",
                    "--force-recreate",
                    *recreated_dependencies,
                ]
            )

        result: RuntimeCommandResult | None = None
        for argv in commands:
            result = await self._execute_logged(
                argv,
                environment,
                timeout_seconds,
                log_path,
            )
            if result.timed_out or result.returncode != 0:
                return result
        if result is None:
            raise RuntimeConfigurationError(
                "runtime dependency startup was requested without dependencies"
            )
        return result

    def _resolve_sources(
        self,
        workspace_root: Path,
        repository: str,
        source_repositories: Sequence[str],
    ) -> tuple[Path, dict[str, Path]]:
        if isinstance(source_repositories, (str, bytes)):
            raise RuntimeConfigurationError(
                "source_repositories must be a sequence of repository names"
            )
        root = self._resolve_workspace_root(workspace_root)
        names = list(dict.fromkeys([repository, *source_repositories]))
        sources: dict[str, Path] = {}
        for name in names:
            binding = self._binding(name)
            try:
                source = (root / binding.workspace_subdir).resolve(strict=True)
                source.relative_to(root)
            except (OSError, ValueError) as exc:
                raise RuntimeConfigurationError(
                    f"runtime repository path escapes or is missing from workspace: {name}"
                ) from exc
            if not source.is_dir():
                raise RuntimeConfigurationError(
                    f"runtime repository path is not a directory: {source}"
                )
            self._assert_git_checkout(source)
            sources[name] = source
        return root, sources

    def _resolve_workspace_root(self, workspace_root: Path) -> Path:
        try:
            root = Path(workspace_root).resolve(strict=True)
        except OSError as exc:
            raise RuntimeConfigurationError(
                f"workspace root does not exist: {workspace_root}"
            ) from exc
        if not root.is_dir():
            raise RuntimeConfigurationError(
                f"workspace root is not a directory: {root}"
            )
        return root

    def _assert_git_checkout(self, source: Path) -> None:
        git_marker = source / ".git"
        if git_marker.is_dir():
            head_path = git_marker / "HEAD"
        elif git_marker.is_file():
            try:
                with git_marker.open("r", encoding="utf-8", errors="replace") as handle:
                    marker = handle.read(4096).strip()
                prefix, git_dir_value = marker.split(":", 1)
                if prefix.strip().casefold() != "gitdir":
                    raise ValueError("invalid worktree marker")
                git_dir = Path(git_dir_value.strip())
                if not git_dir.is_absolute():
                    git_dir = git_marker.parent / git_dir
                git_dir = git_dir.resolve(strict=True)
                head_path = git_dir / "HEAD"
            except (OSError, ValueError) as exc:
                raise RuntimeConfigurationError(
                    f"runtime repository has an invalid Git worktree marker: {source}"
                ) from exc
        else:
            raise RuntimeConfigurationError(
                f"runtime repository is not a Git checkout: {source}"
            )
        if not head_path.is_file():
            raise RuntimeConfigurationError(
                f"runtime repository has no Git HEAD: {source}"
            )

    def _compose_environment(self, sources: Mapping[str, Path]) -> dict[str, str]:
        environment = dict(self.environ)
        for name, source in sources.items():
            environment[self._binding(name).source_env] = str(source)
        return environment

    def _validate_runtime_paths(self) -> None:
        project_directory = self.config.project_directory
        compose_file = self.config.compose_file
        env_file = self.config.env_file
        lock_file = self.config.lock_file
        if (
            project_directory is None
            or compose_file is None
            or env_file is None
            or lock_file is None
            or self.config.project_name is None
        ):
            raise RuntimeConfigurationError(
                "enabled runtime is missing required path/name configuration"
            )
        if not project_directory.is_dir():
            raise RuntimeConfigurationError(
                f"runtime project_directory does not exist: {project_directory}"
            )
        for label, path in (("compose_file", compose_file), ("env_file", env_file)):
            if not path.is_file():
                raise RuntimeConfigurationError(
                    f"runtime {label} does not exist: {path}"
                )

    def _compose_prefix(self) -> list[str]:
        project_directory = self.config.project_directory
        compose_file = self.config.compose_file
        env_file = self.config.env_file
        project_name = self.config.project_name
        if (
            project_directory is None
            or compose_file is None
            or env_file is None
            or project_name is None
        ):
            raise RuntimeConfigurationError(
                "enabled runtime is missing required Compose configuration"
            )
        return [
            *self.config.command,
            "--project-name",
            project_name,
            "--project-directory",
            str(project_directory),
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
        ]

    async def _rendered_config(
        self, environment: Mapping[str, str], log_path: Path
    ) -> tuple[RuntimeCommandResult, Mapping[str, Any] | None]:
        result = await self._execute(
            self._compose_prefix() + ["config", "--format", "json"],
            environment,
            self.config.preview_timeout_seconds,
        )
        if result.timed_out or result.returncode != 0:
            safe_result = _replace_command_output(
                result, redact_compose_error_output(result.output)
            )
            self._append_command(log_path, safe_result)
            return safe_result, None

        self._append_command_summary(
            log_path, result, _RENDERED_CONFIG_OMITTED
        )
        compose_config = self._parse_compose_config(result.output)
        return (
            _replace_command_output(result, _RENDERED_CONFIG_OMITTED),
            compose_config,
        )

    def _parse_compose_config(self, output: str) -> Mapping[str, Any]:
        start = output.find("{")
        end = output.rfind("}")
        if start < 0 or end < start:
            raise RuntimeConfigurationError(
                "Podman Compose config did not return a JSON object"
            )
        parsed = json.loads(output[start : end + 1])
        if not isinstance(parsed, Mapping):
            raise RuntimeConfigurationError(
                "Podman Compose config JSON is not an object"
            )
        return parsed

    def _assert_mount(
        self,
        compose_config: Mapping[str, Any],
        binding: RuntimeRepositoryConfig,
        repository_path: Path,
    ) -> None:
        services = compose_config.get("services")
        if not isinstance(services, Mapping):
            raise RuntimeConfigurationError(
                "Podman Compose config does not contain services"
            )
        service = services.get(binding.service)
        if not isinstance(service, Mapping):
            raise RuntimeConfigurationError(
                f"Podman Compose service is missing: {binding.service}"
            )
        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            raise RuntimeConfigurationError(
                f"Podman Compose service volumes are invalid: {binding.service}"
            )
        expected_target = os.path.normpath(binding.mount_target)
        for volume in volumes:
            source: str | None = None
            target: str | None = None
            volume_type: str | None = None
            if isinstance(volume, Mapping):
                raw_source = volume.get("source")
                raw_target = volume.get("target")
                raw_type = volume.get("type")
                source = raw_source if isinstance(raw_source, str) else None
                target = raw_target if isinstance(raw_target, str) else None
                volume_type = raw_type if isinstance(raw_type, str) else None
            elif isinstance(volume, str):
                pieces = volume.split(":")
                if len(pieces) >= 2:
                    source, target = pieces[0], pieces[1]
            if (
                source
                and target
                and volume_type in {None, "bind"}
                and os.path.normpath(target) == expected_target
                and Path(source).resolve() == repository_path
            ):
                return
        raise RuntimeConfigurationError(
            f"Compose mount for {binding.service}:{binding.mount_target} does not "
            f"resolve to workspace checkout {repository_path}"
        )

    def _shutdown_service_order(
        self,
        compose_config: Mapping[str, Any],
        repositories: Sequence[str],
    ) -> tuple[str, ...]:
        services = compose_config.get("services")
        if not isinstance(services, Mapping):
            raise RuntimeConfigurationError(
                "Podman Compose config does not contain services"
            )

        graph: dict[str, set[str]] = {}
        pending: set[str] = set()
        for repository in repositories:
            binding = self._binding(repository)
            graph.setdefault(binding.service, set()).update(binding.dependencies)
            pending.add(binding.service)
            for dependency in binding.dependencies:
                graph.setdefault(dependency, set())
                pending.add(dependency)

        visited: set[str] = set()
        while pending:
            service_name = min(pending)
            pending.remove(service_name)
            if service_name in visited:
                continue
            service_config = services.get(service_name)
            if not isinstance(service_config, Mapping):
                raise RuntimeConfigurationError(
                    "Shutdown service is missing or malformed in rendered Compose "
                    f"configuration: {service_name}"
                )
            dependencies = self._compose_dependencies(
                service_name, service_config.get("depends_on")
            )
            graph.setdefault(service_name, set()).update(dependencies)
            for dependency in dependencies:
                if dependency not in services:
                    raise RuntimeConfigurationError(
                        f"Compose service {service_name} depends on unknown service "
                        f"{dependency}"
                    )
                graph.setdefault(dependency, set())
                if dependency not in visited:
                    pending.add(dependency)
            visited.add(service_name)

        incoming = {service_name: 0 for service_name in graph}
        for dependencies in graph.values():
            for dependency in dependencies:
                incoming[dependency] += 1
        ready = [
            service_name
            for service_name, count in incoming.items()
            if count == 0
        ]
        heapq.heapify(ready)
        ordered: list[str] = []
        while ready:
            service_name = heapq.heappop(ready)
            ordered.append(service_name)
            for dependency in sorted(graph[service_name]):
                incoming[dependency] -= 1
                if incoming[dependency] == 0:
                    heapq.heappush(ready, dependency)
        if len(ordered) != len(graph):
            raise RuntimeConfigurationError(
                "Compose dependency graph contains a cycle"
            )
        return tuple(ordered)

    def _compose_dependencies(
        self, service_name: str, depends_on: Any
    ) -> tuple[str, ...]:
        if depends_on is None:
            return ()
        if isinstance(depends_on, Mapping):
            dependencies = list(depends_on)
        elif isinstance(depends_on, list):
            dependencies = depends_on
        else:
            raise RuntimeConfigurationError(
                f"Compose service {service_name} has malformed depends_on"
            )
        if any(
            not isinstance(dependency, str)
            or not dependency.strip()
            or "\x00" in dependency
            for dependency in dependencies
        ):
            raise RuntimeConfigurationError(
                f"Compose service {service_name} has malformed depends_on"
            )
        return tuple(sorted(set(dependencies)))

    @asynccontextmanager
    async def _runtime_lock(self) -> AsyncIterator[None]:
        lock_path = self.config.lock_file
        if lock_path is None:
            raise RuntimeConfigurationError("runtime.lock_file is required")
        await asyncio.to_thread(lock_path.parent.mkdir, parents=True, exist_ok=True)
        handle: BinaryIO = await asyncio.to_thread(open, lock_path, "a+b")
        deadline = time.monotonic() + self.config.lock_timeout_seconds
        acquired = False
        try:
            while not acquired:
                try:
                    await asyncio.to_thread(
                        fcntl.flock, handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeTimeoutError(
                            f"timed out acquiring runtime lock: {lock_path}"
                        )
                    await asyncio.sleep(0.1)
            yield
        finally:
            if acquired:
                await asyncio.to_thread(
                    fcntl.flock, handle.fileno(), fcntl.LOCK_UN
                )
            await asyncio.to_thread(handle.close)

    @asynccontextmanager
    async def _optional_runtime_lock(
        self, acquire: bool
    ) -> AsyncIterator[None]:
        if not acquire:
            yield
            return
        async with self._runtime_lock():
            yield

    async def _execute_logged(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
        log_path: Path,
    ) -> RuntimeCommandResult:
        result = await self._execute(argv, environment, timeout_seconds)
        safe_result = _replace_command_output(
            result, redact_compose_error_output(result.output)
        )
        self._append_command(log_path, safe_result)
        return safe_result

    async def _execute(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> RuntimeCommandResult:
        started_at = _utcnow()
        safe_argv = sanitize_argv(argv)
        try:
            process = await self._process_factory(
                *argv,
                cwd=str(self.config.project_directory),
                env=dict(environment),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeCommandError(
                f"failed to start runtime command: {exc}", safe_argv
            ) from exc

        capture = _OutputCapture(bytearray(), bytearray())
        reader = asyncio.create_task(
            self._read_output(process.stdout, capture)
        )
        timed_out = False
        output_stream_remained_open = False
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                await self._terminate_process(process)
            output_stream_remained_open = await self._drain_output_reader(
                reader, process.stdout
            )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                self._cancel_process_and_output_reader(
                    process, reader, process.stdout
                )
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            try:
                cleanup.result()
            except Exception:
                pass
            raise

        diagnostic = (
            b"\n" + _OPEN_OUTPUT_STREAM_MARKER.encode("ascii") + b"\n"
            if output_stream_remained_open
            else b""
        )
        retained_output_limit = self.config.max_output_bytes - len(diagnostic)
        if capture.total_bytes > retained_output_limit:
            available = retained_output_limit - len(_OUTPUT_TRUNCATION_MARKER)
            head_length = available // 2
            tail_length = available - head_length
            output_bytes = (
                bytes(capture.head[:head_length])
                + _OUTPUT_TRUNCATION_MARKER
                + bytes(capture.tail[-tail_length:])
            )
        else:
            output_bytes = bytes(capture.head) + bytes(capture.tail)
        output = _decode_runtime_output(output_bytes + diagnostic)
        return RuntimeCommandResult(
            argv=safe_argv,
            returncode=process.returncode,
            output=output,
            timed_out=timed_out,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    async def _read_output(
        self,
        stream: _ReadableStream | None,
        capture: _OutputCapture,
    ) -> None:
        if stream is None:
            return
        head_limit = self.config.max_output_bytes // 2
        tail_limit = self.config.max_output_bytes - head_limit
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            capture.total_bytes += len(chunk)
            head_remaining = head_limit - len(capture.head)
            if head_remaining > 0:
                capture.head.extend(chunk[:head_remaining])
                chunk = chunk[head_remaining:]
            if chunk:
                capture.tail.extend(chunk)
                excess = len(capture.tail) - tail_limit
                if excess > 0:
                    del capture.tail[:excess]

    async def _drain_output_reader(
        self,
        reader: asyncio.Task[None],
        stream: _ReadableStream | None,
    ) -> bool:
        if reader.done():
            await reader
            return False
        done, _ = await asyncio.wait(
            {reader}, timeout=self.config.termination_grace_seconds
        )
        if done:
            await reader
            return False
        self._close_output_stream(stream)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        return True

    async def _cancel_process_and_output_reader(
        self,
        process: _Process,
        reader: asyncio.Task[None],
        stream: _ReadableStream | None,
    ) -> None:
        try:
            await self._terminate_process(process)
        finally:
            self._close_output_stream(stream)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @staticmethod
    def _close_output_stream(stream: _ReadableStream | None) -> None:
        transport = getattr(stream, "_transport", None)
        close = getattr(transport, "close", None)
        if close is not None:
            close()

    async def _terminate_process(self, process: _Process) -> None:
        if process.returncode is not None:
            return
        try:
            if self._native_process_factory:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(
                process.wait(), self.config.termination_grace_seconds
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            if self._native_process_factory:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(
                process.wait(), self.config.termination_grace_seconds
            )
        except asyncio.TimeoutError:
            return

    def _prepare_log(self, root: Path, repository: str, operation: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", repository)
        invocation = (
            _utcnow().strftime("%Y%m%dT%H%M%S.%fZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        log_path = (
            root
            / ".symphony"
            / "runtime"
            / f"{safe_name}-{operation}-{invocation}.log"
        )
        return create_runtime_artifact(log_path)

    def _append_command(
        self, log_path: Path, result: RuntimeCommandResult
    ) -> None:
        heading = "$ " + " ".join(result.argv) + "\n"
        self._append_log(log_path, heading + result.output)

    def _append_command_summary(
        self,
        log_path: Path,
        result: RuntimeCommandResult,
        summary: str,
    ) -> None:
        heading = "$ " + " ".join(result.argv) + "\n"
        self._append_log(log_path, heading + summary)

    def _append_log(self, log_path: Path, text: str) -> None:
        with open_runtime_artifact_for_update(log_path) as handle:
            existing = handle.read(self.config.max_output_bytes + 1)
            addition = text.encode("utf-8", errors="replace")
            separator = (
                b"\n" if existing and not existing.endswith(b"\n") else b""
            )
            combined = existing + separator + addition
            if len(combined) <= self.config.max_output_bytes:
                bounded = combined
            elif not existing:
                bounded = _bounded_log_entry(
                    addition,
                    self.config.max_output_bytes,
                )
            else:
                available = (
                    self.config.max_output_bytes
                    - len(_EARLIER_LOG_TRUNCATION_MARKER)
                )
                bounded = _EARLIER_LOG_TRUNCATION_MARKER + _bounded_log_entry(
                    addition,
                    available,
                )
            handle.seek(0)
            handle.write(bounded)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())

    def _append_log_best_effort(self, log_path: Path, text: str) -> None:
        try:
            self._append_log(log_path, text)
        except OSError:
            return

    def _verification_result(
        self,
        *,
        repository: str,
        profile: str,
        status: VerificationStatus,
        repository_path: Path | None,
        started_at: datetime,
        log_path: Path | None,
        command: RuntimeCommandResult,
        message: str,
    ) -> RuntimeVerificationResult:
        return RuntimeVerificationResult(
            repository=repository,
            profile=profile,
            status=status,
            argv=command.argv,
            repository_path=repository_path,
            started_at=started_at,
            finished_at=_utcnow(),
            returncode=command.returncode,
            output=command.output,
            log_path=log_path,
            message=message,
        )

    def _preview_result(
        self,
        repository: str,
        status: PreviewStatus,
        repository_path: Path | None,
        started_at: datetime,
        log_path: Path | None,
        command: RuntimeCommandResult,
        message: str,
    ) -> RuntimePreviewResult:
        return RuntimePreviewResult(
            repository=repository,
            status=status,
            argv=command.argv,
            repository_path=repository_path,
            started_at=started_at,
            finished_at=_utcnow(),
            returncode=command.returncode,
            output=command.output,
            log_path=log_path,
            message=message,
        )

    def _shutdown_result(
        self,
        repositories: tuple[str, ...],
        services: tuple[str, ...],
        status: ShutdownStatus,
        started_at: datetime,
        log_path: Path | None,
        command: RuntimeCommandResult,
        message: str,
    ) -> RuntimeShutdownResult:
        return RuntimeShutdownResult(
            repositories=repositories,
            services=services,
            status=status,
            argv=command.argv,
            started_at=started_at,
            finished_at=_utcnow(),
            returncode=command.returncode,
            output=command.output,
            log_path=log_path,
            message=message,
        )


def create_runtime_artifact(artifact_path: Path) -> Path:
    """Create one empty runtime artifact without following workspace links."""

    workspace_root, filename, normalized_path = _runtime_artifact_location(
        artifact_path
    )
    with _runtime_artifact_directory(workspace_root, create=True) as directory_fd:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag(),
                0o600,
                dir_fd=directory_fd,
            )
            _validate_runtime_artifact_descriptor(descriptor, filename)
        except OSError as exc:
            raise RuntimeArtifactError(
                f"could not safely create runtime artifact {normalized_path}: {exc}"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return normalized_path


@contextmanager
def open_runtime_artifact_for_update(
    artifact_path: Path,
) -> Iterator[BinaryIO]:
    """Open an existing runtime artifact for bounded in-place updates."""

    workspace_root, filename, normalized_path = _runtime_artifact_location(
        artifact_path
    )
    with _runtime_artifact_directory(workspace_root, create=False) as directory_fd:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                filename,
                os.O_RDWR | _nofollow_flag() | _nonblock_flag(),
                dir_fd=directory_fd,
            )
            _validate_runtime_artifact_descriptor(descriptor, filename)
            with os.fdopen(descriptor, "r+b", closefd=True) as handle:
                descriptor = None
                yield handle
        except RuntimeArtifactError:
            raise
        except OSError as exc:
            raise RuntimeArtifactError(
                f"could not safely update runtime artifact {normalized_path}: {exc}"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)


def write_runtime_artifact_bytes(artifact_path: Path, content: bytes) -> Path:
    """Atomically replace a runtime artifact without following workspace links."""

    workspace_root, filename, normalized_path = _runtime_artifact_location(
        artifact_path
    )
    temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    with _runtime_artifact_directory(workspace_root, create=True) as directory_fd:
        temporary_created = False
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _nofollow_flag(),
                0o600,
                dir_fd=directory_fd,
            )
            temporary_created = True
            try:
                _validate_runtime_artifact_descriptor(
                    descriptor,
                    temporary_name,
                )
            except OSError:
                os.close(descriptor)
                raise
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            target_descriptor: int | None = None
            try:
                target_descriptor = os.open(
                    filename,
                    os.O_RDONLY | _nofollow_flag() | _nonblock_flag(),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                pass
            else:
                _validate_runtime_artifact_descriptor(
                    target_descriptor,
                    filename,
                )
            finally:
                if target_descriptor is not None:
                    os.close(target_descriptor)

            os.replace(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_created = False
            os.fsync(directory_fd)
        except OSError as exc:
            raise RuntimeArtifactError(
                f"could not safely write runtime artifact {normalized_path}: {exc}"
            ) from exc
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
    return normalized_path


def _runtime_artifact_location(
    artifact_path: Path,
) -> tuple[Path, str, Path]:
    normalized_path = Path(os.path.abspath(os.fspath(artifact_path)))
    runtime_directory = normalized_path.parent
    symphony_directory = runtime_directory.parent
    if (
        runtime_directory.name != "runtime"
        or symphony_directory.name != ".symphony"
        or normalized_path.name in {"", ".", ".."}
    ):
        raise RuntimeArtifactError(
            "runtime artifact path must be directly inside .symphony/runtime: "
            f"{artifact_path}"
        )
    return symphony_directory.parent, normalized_path.name, normalized_path


@contextmanager
def _runtime_artifact_directory(
    workspace_root: Path,
    *,
    create: bool,
) -> Iterator[int]:
    directory_flags = os.O_RDONLY | _directory_flag() | _nofollow_flag()
    descriptors: list[int] = []
    try:
        parent_fd = os.open(
            os.path.abspath(os.fspath(workspace_root)),
            directory_flags,
        )
        descriptors.append(parent_fd)
        for component in (".symphony", "runtime"):
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            parent_fd = os.open(
                component,
                directory_flags,
                dir_fd=parent_fd,
            )
            descriptors.append(parent_fd)
        yield parent_fd
    except RuntimeArtifactError:
        raise
    except OSError as exc:
        raise RuntimeArtifactError(
            "could not safely open workspace .symphony/runtime directory: "
            f"{exc}"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_runtime_artifact_descriptor(descriptor: int, label: str) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeArtifactError(f"runtime artifact is not a regular file: {label}")
    if metadata.st_nlink != 1:
        raise RuntimeArtifactError(f"runtime artifact must not be hard-linked: {label}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeArtifactError(
            f"runtime artifact is not owned by the current user: {label}"
        )


def _nofollow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if not flag:
        raise RuntimeArtifactError(
            "secure runtime artifact writes are unavailable on this platform"
        )
    return flag


def _directory_flag() -> int:
    flag = getattr(os, "O_DIRECTORY", 0)
    if not flag:
        raise RuntimeArtifactError(
            "secure runtime artifact writes are unavailable on this platform"
        )
    return flag


def _nonblock_flag() -> int:
    flag = getattr(os, "O_NONBLOCK", 0)
    if not flag:
        raise RuntimeArtifactError(
            "secure runtime artifact writes are unavailable on this platform"
        )
    return flag


def sanitize_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Redact values carried by Compose -e/--env command options."""

    sanitized: list[str] = []
    redact_next = False
    for argument in argv:
        if redact_next:
            name = argument.split("=", 1)[0]
            sanitized.append(f"{name}=<redacted>")
            redact_next = False
        elif argument in {"-e", "--env"}:
            sanitized.append(argument)
            redact_next = True
        elif argument.startswith("--env="):
            name = argument.split("=", 1)[1].split("=", 1)[0]
            sanitized.append(f"--env={name}=<redacted>")
        else:
            sanitized.append(argument)
    return tuple(sanitized)


def redact_compose_error_output(output: str) -> str:
    """Remove obvious credentials from a failed Compose config diagnostic."""

    redacted = _SENSITIVE_JSON_VALUE.sub(
        lambda match: match.group(1) + '"<redacted>"',
        output,
    )
    redacted = _SENSITIVE_ENV_ASSIGNMENT.sub(
        lambda match: match.group("prefix") + "<redacted>",
        redacted,
    )
    return _CREDENTIAL_URL.sub(
        lambda match: match.group("scheme") + "<redacted>@",
        redacted,
    )


def runtime_environment_blocker_message(output: str) -> str | None:
    """Return an actionable message for known non-test Podman failures.

    Compose providers do not preserve Podman's exit-code distinction: a host
    networking or runtime-directory error commonly arrives as exit code 1,
    which is also pytest's ordinary failure code. Match only specific host
    signatures so a repository assertion remains ``test_failed``.
    """

    normalized = " ".join(output.casefold().split())
    if (
        "dnsname" in normalized
        and "permission denied" in normalized
        and ("cni plugin" in normalized or "plugin type=" in normalized)
    ):
        return _DNSNAME_PERMISSION_HINT
    if (
        "libpod" in normalized
        and (
            "read-only file system" in normalized
            or "permission denied" in normalized
        )
    ) or (
        "xdg_runtime_dir" in normalized
        and ("not writable" in normalized or "permission denied" in normalized)
    ):
        return _ROOTLESS_RUNTIME_PERMISSION_HINT
    return None


def _replace_command_output(
    result: RuntimeCommandResult, output: str
) -> RuntimeCommandResult:
    return replace(result, output=output)


def _bounded_log_entry(content: bytes, limit: int) -> bytes:
    if len(content) <= limit:
        return content
    available = limit - len(_LOG_ENTRY_TRUNCATION_MARKER)
    if available <= 0:
        return _LOG_ENTRY_TRUNCATION_MARKER[:limit]
    prefix_length = available // 2
    suffix_length = available - prefix_length
    return (
        content[:prefix_length]
        + _LOG_ENTRY_TRUNCATION_MARKER
        + content[-suffix_length:]
    )


def _decode_runtime_output(content: bytes) -> str:
    """Decode output while replacing each invalid byte with one bounded byte."""

    return (
        content.decode("utf-8", errors="surrogateescape")
        .encode("utf-8", errors="replace")
        .decode("utf-8")
    )


def _empty_command() -> RuntimeCommandResult:
    now = _utcnow()
    return RuntimeCommandResult(
        argv=(),
        returncode=None,
        output="",
        timed_out=False,
        started_at=now,
        finished_at=now,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
