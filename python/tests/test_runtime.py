from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from symphony_jira.config import RuntimeConfig
from symphony_jira.runtime import (
    RuntimeArtifactError,
    RuntimeManager,
    write_runtime_artifact_bytes,
)


COMPOSE_SECRET = "compose-config-secret-value"
RENDERED_CONFIG_OMITTED = "[rendered Compose configuration omitted]"


@dataclass(frozen=True)
class FakeResponse:
    returncode: int
    output: str = ""


class FakeProcess:
    def __init__(self, response: FakeResponse) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self._expected_returncode = response.returncode
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(response.output.encode())
        self.stdout.feed_eof()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self._expected_returncode
        return self._expected_returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class HangingProcess:
    def __init__(self) -> None:
        self.pid = 12346
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.terminated = False
        self.killed = False
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.feed_eof()
        self._finished.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self._finished.set()


class ExitedWithOpenStdoutProcess:
    def __init__(self, output: bytes = b"complete\n") -> None:
        self.pid = 12347
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class HangingProcessWithOpenStdout:
    def __init__(self) -> None:
        self.pid = 12348
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.terminated = False
        self.killed = False
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._finished.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._finished.set()


class FakeProcessFactory:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProcess:
        if not self.responses:
            raise AssertionError(f"unexpected process invocation: {argv}")
        self.calls.append((argv, kwargs))
        process = FakeProcess(self.responses.pop(0))
        self.processes.append(process)
        return process


class RuntimeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_starts_service_then_executes_fixed_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp), include_foyr=True)
            compose_json = rendered_config(root, include_foyr=True)
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, compose_json),
                    FakeResponse(0, "service ready"),
                    FakeResponse(0, "3 passed"),
                ]
            )
            manager = RuntimeManager(
                config,
                environ={
                    "INHERITED": "yes",
                    "CUSTOM_JIRA_SECRET": "must-not-leak",
                    "JIRA_BASE_URL": "must-not-leak",
                    "PROJECT_JIRA_TOKEN": "must-not-leak",
                    "PROJECT_JIRA_EMAIL": "must-not-leak",
                },
                excluded_environment_names={"CUSTOM_JIRA_SECRET"},
                process_factory=factory,
            )

            result = await manager.verify(
                root / "workspace",
                "cpm",
                source_repositories=["foyr"],
            )

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.output, "3 passed")
            self.assertEqual(result.repository_path, root / "workspace/cpm")
            self.assertIsNotNone(result.log_path)
            assert result.log_path is not None
            self.assertTrue(result.log_path.is_file())
            log_text = result.log_path.read_text()
            self.assertIn("3 passed", log_text)
            self.assertIn(RENDERED_CONFIG_OMITTED, log_text)
            self.assertNotIn(COMPOSE_SECRET, log_text)
            self.assertNotIn(COMPOSE_SECRET, result.output)
            self.assertEqual((root / "runtime/.env").read_text(), "UNCHANGED=1\n")

            config_call, startup_call, test_call = factory.calls
            self.assertEqual(config_call[0][-3:], ("config", "--format", "json"))
            self.assertEqual(
                startup_call[0][-5:],
                ("up", "-d", "--wait", "--force-recreate", "cpm"),
            )
            self.assertEqual(
                test_call[0][-10:],
                (
                    "exec",
                    "-T",
                    "-e",
                    "APP_CONFIG=test.yml",
                    "--workdir",
                    "/src",
                    "cpm",
                    "pytest",
                    "-q",
                    "tests",
                ),
            )
            self.assertNotIn("run", test_call[0])
            self.assertNotIn("--rm", test_call[0])
            self.assertNotIn("--no-deps", test_call[0])
            self.assertNotIn("shell", test_call[1])
            for _, kwargs in factory.calls:
                self.assertEqual(kwargs["env"]["CPM_SRC"], str(root / "workspace/cpm"))
                self.assertEqual(
                    kwargs["env"]["FOYR_SRC"], str(root / "workspace/foyr")
                )
                self.assertEqual(kwargs["env"]["INHERITED"], "yes")
                self.assertNotIn("CUSTOM_JIRA_SECRET", kwargs["env"])
                self.assertNotIn("JIRA_BASE_URL", kwargs["env"])
                self.assertNotIn("PROJECT_JIRA_TOKEN", kwargs["env"])
                self.assertNotIn("PROJECT_JIRA_EMAIL", kwargs["env"])
            self.assertIn("APP_CONFIG=<redacted>", result.argv)
            self.assertNotIn("APP_CONFIG=test.yml", result.argv)

    async def test_verify_uses_config_key_for_nested_workspace_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            repository_path = root / "workspace/services/api"
            (repository_path / ".git").mkdir(parents=True)
            (repository_path / ".git/HEAD").write_text(
                "ref: refs/heads/feature/T-1\n"
            )
            binding = config.repositories.pop("cpm")
            binding.workspace_subdir = Path("services/api")
            config.repositories["backend"] = binding
            config = RuntimeConfig.model_validate(config.model_dump())
            compose_json = rendered_config(root).replace(
                str(root / "workspace/cpm"),
                str(repository_path),
            )
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, compose_json),
                    FakeResponse(0, "service ready"),
                    FakeResponse(0, "3 passed"),
                ]
            )

            result = await RuntimeManager(
                config,
                environ={},
                process_factory=factory,
            ).verify(root / "workspace", "backend")

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.repository, "backend")
            self.assertEqual(result.repository_path, repository_path)
            for _, kwargs in factory.calls:
                self.assertEqual(kwargs["env"]["CPM_SRC"], str(repository_path))

    async def test_workspace_dependency_and_target_are_deduped_and_recreated_together(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            binding = config.repositories["cpm"]
            binding.force_recreate_dependencies = ["ibis", "ibis"]
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(0, "ibis and cpm recreated and healthy"),
                    FakeResponse(0, "3 passed"),
                ]
            )

            result = await RuntimeManager(
                config,
                environ={},
                process_factory=factory,
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "passed")
            self.assertEqual(
                factory.calls[1][0][-6:],
                (
                    "up",
                    "-d",
                    "--wait",
                    "--force-recreate",
                    "ibis",
                    "cpm",
                ),
            )
            self.assertNotIn("db", factory.calls[1][0])
            self.assertNotIn("cache", factory.calls[1][0])
            self.assertIn("exec", factory.calls[2][0])

    async def test_failed_workspace_dependency_recreation_blocks_tests_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            binding = config.repositories["cpm"]
            binding.dependencies = ["ibis"]
            binding.force_recreate_dependencies = ["ibis"]
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(1, "container ibis is unhealthy"),
                ]
            )

            result = await RuntimeManager(
                config,
                environ={},
                process_factory=factory,
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "environment_blocked")
            self.assertEqual(len(factory.calls), 2)
            self.assertEqual(
                factory.calls[1][0][-6:],
                (
                    "up",
                    "-d",
                    "--wait",
                    "--force-recreate",
                    "ibis",
                    "cpm",
                ),
            )

    async def test_preview_force_recreates_workspace_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            config.repositories["cpm"].force_recreate_dependencies = ["ibis"]
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(0, "infrastructure ready"),
                    FakeResponse(0, "ibis recreated and healthy"),
                    FakeResponse(0, "preview ready"),
                ]
            )

            result = await RuntimeManager(
                config,
                environ={},
                process_factory=factory,
            ).start_preview(root / "workspace", "cpm")

            self.assertEqual(result.status, "started")
            self.assertEqual(
                factory.calls[2][0][-5:],
                ("up", "-d", "--wait", "--force-recreate", "ibis"),
            )
            self.assertEqual(
                factory.calls[3][0][-6:],
                (
                    "up",
                    "-d",
                    "--wait",
                    "--no-deps",
                    "--force-recreate",
                    "cpm",
                ),
            )

    async def test_target_arguments_are_literal_argv_and_replace_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(0, "service ready"),
                    FakeResponse(0, "ok"),
                ]
            )
            manager = RuntimeManager(config, environ={}, process_factory=factory)

            result = await manager.verify(
                root / "workspace",
                "cpm",
                target_args=["tests/unit/test_one.py", "-k", "x; touch /tmp/nope"],
            )

            self.assertEqual(result.status, "passed")
            argv, kwargs = factory.calls[-1]
            self.assertEqual(
                argv[-5:],
                (
                    "pytest",
                    "-q",
                    "tests/unit/test_one.py",
                    "-k",
                    "x; touch /tmp/nope",
                ),
            )
            self.assertNotIn("shell", kwargs)

    async def test_mount_mismatch_and_missing_git_are_environment_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            wrong = json.dumps(
                {
                    "services": {
                        "cpm": {
                            "environment": {
                                "DATABASE_PASSWORD": COMPOSE_SECRET,
                            },
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(root / "somewhere-else"),
                                    "target": "/src",
                                }
                            ]
                        }
                    }
                }
            )
            factory = FakeProcessFactory([FakeResponse(0, wrong)])
            manager = RuntimeManager(config, environ={}, process_factory=factory)

            mismatch = await manager.verify(root / "workspace", "cpm")
            self.assertEqual(mismatch.status, "environment_blocked")
            self.assertIn("does not resolve", mismatch.message)
            self.assertEqual(mismatch.output, RENDERED_CONFIG_OMITTED)
            self.assertNotIn(COMPOSE_SECRET, mismatch.output)
            assert mismatch.log_path is not None
            mismatch_log = mismatch.log_path.read_text()
            self.assertIn(RENDERED_CONFIG_OMITTED, mismatch_log)
            self.assertNotIn(COMPOSE_SECRET, mismatch_log)
            self.assertEqual(len(factory.calls), 1)

            (root / "workspace/cpm/.git/HEAD").unlink()
            (root / "workspace/cpm/.git").rmdir()
            no_git_factory = FakeProcessFactory([])
            no_git = await RuntimeManager(
                config, environ={}, process_factory=no_git_factory
            ).verify(root / "workspace", "cpm")
            self.assertEqual(no_git.status, "environment_blocked")
            self.assertIn("not a Git checkout", no_git.message)
            self.assertEqual(no_git_factory.calls, [])

    async def test_preview_mount_failure_does_not_expose_rendered_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            rendered = json.dumps(
                {
                    "services": {
                        "cpm": {
                            "environment": {
                                "DATABASE_DSN": (
                                    f"oracle://user:{COMPOSE_SECRET}@db/service"
                                )
                            },
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(root / "wrong-checkout"),
                                    "target": "/src",
                                }
                            ],
                        }
                    }
                }
            )
            factory = FakeProcessFactory([FakeResponse(0, rendered)])

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).start_preview(
                root / "workspace",
                "cpm",
                start_dependencies=False,
            )

            self.assertEqual(result.status, "environment_blocked")
            self.assertEqual(result.output, RENDERED_CONFIG_OMITTED)
            self.assertNotIn(COMPOSE_SECRET, result.output)
            assert result.log_path is not None
            log_text = result.log_path.read_text()
            self.assertIn(RENDERED_CONFIG_OMITTED, log_text)
            self.assertNotIn(COMPOSE_SECRET, log_text)

    async def test_failed_config_diagnostic_redacts_obvious_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            diagnostic = (
                "DATABASE_PASSWORD=hunter2\n"
                "DATABASE_DSN=oracle://user:url-secret@db/service\n"
                '{"API_TOKEN": "json-secret"}\n'
                "compose provider returned an error\n"
            )
            factory = FakeProcessFactory([FakeResponse(1, diagnostic)])

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "environment_blocked")
            self.assertIn("compose provider returned an error", result.output)
            for secret in ("hunter2", "url-secret", "json-secret"):
                self.assertNotIn(secret, result.output)
            assert result.log_path is not None
            log_text = result.log_path.read_text()
            self.assertIn("compose provider returned an error", log_text)
            for secret in ("hunter2", "url-secret", "json-secret"):
                self.assertNotIn(secret, log_text)

    async def test_dnsname_permission_denial_has_non_retryable_host_diagnostic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(
                        1,
                        'Error response from daemon: plugin type="dnsname" '
                        "failed (add): cni plugin dnsname failed: permission denied\n",
                    ),
                ]
            )

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "environment_blocked")
            self.assertEqual(result.returncode, 1)
            self.assertIn("Host Podman CNI dnsname", result.message)
            self.assertIn("Repository tests did not run", result.message)
            self.assertIn("not safe to retry automatically", result.message)
            self.assertIn("allow only SIGHUP", result.message)
            self.assertIn("peer podman", result.message)
            self.assertIn(
                "/etc/apparmor.d/local/usr.sbin.dnsmasq", result.message
            )
            self.assertEqual(len(factory.calls), 2)

    async def test_preview_dependency_dnsname_denial_has_host_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(
                        1,
                        'plugin type="dnsname" failed (add): cni plugin '
                        "dnsname failed: permission denied",
                    ),
                ]
            )

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).start_preview(root / "workspace", "cpm")

            self.assertEqual(result.status, "environment_blocked")
            self.assertIn("allow only SIGHUP", result.message)
            self.assertIn("peer podman", result.message)
            self.assertEqual(len(factory.calls), 2)

    async def test_compose_exec_dnsname_denial_is_not_a_test_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(0, "service ready"),
                    FakeResponse(
                        1,
                        "CNI plugin DNSNAME failed (add): PERMISSION DENIED\n",
                    ),
                ]
            )

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "environment_blocked")
            self.assertIn("host-policy failure", result.message)

    async def test_read_only_rootless_runtime_has_sandbox_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            factory = FakeProcessFactory(
                [
                    FakeResponse(
                        1,
                        "set sticky bit on: chmod /run/user/1000/libpod: "
                        "read-only file system\n",
                    )
                ]
            )

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "environment_blocked")
            self.assertIn("cannot write its runtime directory", result.message)
            self.assertIn("writable XDG_RUNTIME_DIR", result.message)
            self.assertIn("outside a read-only sandbox", result.message)

    async def test_failed_test_echo_does_not_expose_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            config.verification_profiles["pytest"].environment["API_TOKEN"] = (
                "super-secret"
            )
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(0, "service ready"),
                    FakeResponse(
                        1,
                        "provider argv: -e API_TOKEN=super-secret\n"
                        "ordinary assertion failed\n",
                    ),
                ]
            )

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "test_failed")
            self.assertIn("ordinary assertion failed", result.output)
            self.assertNotIn("super-secret", result.output)
            self.assertFalse(
                any("super-secret" in argument for argument in result.argv)
            )
            assert result.log_path is not None
            log_text = result.log_path.read_text()
            self.assertIn("ordinary assertion failed", log_text)
            self.assertNotIn("super-secret", log_text)

    async def test_shutdown_stops_dependency_closure_in_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp), include_foyr=True)
            factory = FakeProcessFactory(
                [
                    FakeResponse(
                        0,
                        shutdown_rendered_config(root, include_foyr=True),
                    ),
                    FakeResponse(0, "services stopped"),
                ]
            )
            manager = RuntimeManager(config, environ={}, process_factory=factory)

            result = await manager.shutdown(
                root / "workspace",
                ["cpm"],
                source_repositories=["foyr"],
            )

            self.assertEqual(result.status, "stopped")
            self.assertEqual(result.repositories, ("cpm",))
            self.assertEqual(result.services, ("cpm", "cache", "ibis", "db"))
            self.assertEqual(
                factory.calls[-1][0][-7:],
                (
                    "stop",
                    "--timeout",
                    "120",
                    "cpm",
                    "cache",
                    "ibis",
                    "db",
                ),
            )
            for argv, kwargs in factory.calls:
                self.assertNotIn("shell", kwargs)
                self.assertNotIn("down", argv)
                self.assertNotIn("rm", argv)
                self.assertEqual(
                    kwargs["env"]["CPM_SRC"], str(root / "workspace/cpm")
                )
                self.assertEqual(
                    kwargs["env"]["FOYR_SRC"], str(root / "workspace/foyr")
                )
            assert result.log_path is not None
            log_text = result.log_path.read_text()
            self.assertIn(RENDERED_CONFIG_OMITTED, log_text)
            self.assertNotIn(COMPOSE_SECRET, log_text)

    async def test_shutdown_rejects_unknown_and_malformed_depends_on(self) -> None:
        cases = (
            (["missing"], "unknown service missing"),
            ("db", "malformed depends_on"),
            ([123], "malformed depends_on"),
        )
        for depends_on, expected_message in cases:
            with self.subTest(depends_on=depends_on):
                with tempfile.TemporaryDirectory() as tmp:
                    root, config = make_runtime(Path(tmp))
                    rendered = shutdown_rendered_config(
                        root, cpm_depends_on=depends_on
                    )
                    factory = FakeProcessFactory([FakeResponse(0, rendered)])

                    result = await RuntimeManager(
                        config, environ={}, process_factory=factory
                    ).shutdown(root / "workspace", ["cpm"])

                    self.assertEqual(result.status, "environment_blocked")
                    self.assertIn(expected_message, result.message)
                    self.assertEqual(result.output, RENDERED_CONFIG_OMITTED)
                    self.assertEqual(len(factory.calls), 1)
                    assert result.log_path is not None
                    self.assertNotIn(
                        COMPOSE_SECRET, result.log_path.read_text()
                    )

    async def test_shutdown_failure_is_sanitized_and_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, shutdown_rendered_config(root)),
                    FakeResponse(
                        1,
                        "provider argv: -e API_TOKEN=super-secret\n"
                        "stop failed\n",
                    ),
                ]
            )

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).shutdown(root / "workspace", ["cpm"])

            self.assertEqual(result.status, "environment_blocked")
            self.assertEqual(result.returncode, 1)
            self.assertIn("stop failed", result.output)
            self.assertNotIn("super-secret", result.output)
            self.assertEqual(result.services, ("cpm", "cache", "ibis", "db"))
            assert result.log_path is not None
            self.assertNotIn("super-secret", result.log_path.read_text())

    async def test_shutdown_dnsname_denial_has_host_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, shutdown_rendered_config(root)),
                    FakeResponse(
                        1,
                        'plugin type="dnsname" failed (delete): cni plugin '
                        "dnsname failed: permission denied",
                    ),
                ]
            )

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).shutdown(root / "workspace", ["cpm"])

            self.assertEqual(result.status, "environment_blocked")
            self.assertIn("allow only SIGHUP", result.message)
            self.assertIn("peer podman", result.message)

    async def test_shutdown_with_no_repositories_is_locked_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            factory = FakeProcessFactory([])

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).shutdown(root / "workspace", [])

            self.assertEqual(result.status, "stopped")
            self.assertEqual(result.repositories, ())
            self.assertEqual(result.services, ())
            self.assertEqual(result.argv, ())
            self.assertEqual(factory.calls, [])
            assert config.lock_file is not None
            self.assertTrue(config.lock_file.is_file())
            assert result.log_path is not None
            self.assertIn(
                "No runtime services selected", result.log_path.read_text()
            )

    async def test_shutdown_timeout_and_cancellation_terminate_stop_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            config.preview_timeout_seconds = 0.01
            rendered = FakeProcess(
                FakeResponse(0, shutdown_rendered_config(root))
            )
            timed_out_process = HangingProcess()
            processes = [rendered, timed_out_process]

            async def timeout_factory(*argv: str, **kwargs: Any):
                return processes.pop(0)

            timed_out = await RuntimeManager(
                config, environ={}, process_factory=timeout_factory
            ).shutdown(root / "workspace", ["cpm"])

            self.assertEqual(timed_out.status, "environment_blocked")
            self.assertIn("timed out", timed_out.message)
            self.assertTrue(timed_out_process.terminated)

            config.preview_timeout_seconds = 60
            rendered = FakeProcess(
                FakeResponse(0, shutdown_rendered_config(root))
            )
            cancelled_process = HangingProcess()
            stop_started = asyncio.Event()
            invocation = 0

            async def cancellation_factory(*argv: str, **kwargs: Any):
                nonlocal invocation
                invocation += 1
                if invocation == 1:
                    return rendered
                stop_started.set()
                return cancelled_process

            manager = RuntimeManager(
                config, environ={}, process_factory=cancellation_factory
            )
            task = asyncio.create_task(
                manager.shutdown(root / "workspace", ["cpm"])
            )
            await stop_started.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled_process.terminated)
            async with manager._runtime_lock():
                pass

    async def test_test_failure_is_distinct_from_unexecutable_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            for returncode, expected in (
                (1, "test_failed"),
                (125, "environment_blocked"),
                (126, "environment_blocked"),
                (127, "environment_blocked"),
            ):
                with self.subTest(returncode=returncode):
                    factory = FakeProcessFactory(
                        [
                            FakeResponse(0, rendered_config(root)),
                            FakeResponse(0, "service ready"),
                            FakeResponse(returncode, "failed"),
                        ]
                    )
                    result = await RuntimeManager(
                        config, environ={}, process_factory=factory
                    ).verify(root / "workspace", "cpm")
                    self.assertEqual(result.status, expected)

    async def test_verify_many_injects_all_sources_and_keeps_unique_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp), include_foyr=True)
            compose_json = rendered_config(root, include_foyr=True)
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, compose_json),
                    FakeResponse(0, "cpm ready"),
                    FakeResponse(0, "cpm passed"),
                    FakeResponse(0, compose_json),
                    FakeResponse(0, "foyr ready"),
                    FakeResponse(0, "foyr passed"),
                ]
            )
            manager = RuntimeManager(config, environ={}, process_factory=factory)

            results = await manager.verify_many(
                root / "workspace", ["cpm", "foyr"]
            )

            self.assertEqual([result.status for result in results], ["passed", "passed"])
            self.assertNotEqual(results[0].log_path, results[1].log_path)
            for _, kwargs in factory.calls:
                self.assertEqual(kwargs["env"]["CPM_SRC"], str(root / "workspace/cpm"))
                self.assertEqual(
                    kwargs["env"]["FOYR_SRC"], str(root / "workspace/foyr")
                )

            second_factory = FakeProcessFactory(
                [
                    FakeResponse(0, compose_json),
                    FakeResponse(0, "cpm ready again"),
                    FakeResponse(0, "passed again"),
                ]
            )
            second = await RuntimeManager(
                config, environ={}, process_factory=second_factory
            ).verify(root / "workspace", "cpm", source_repositories=["foyr"])
            self.assertNotEqual(results[0].log_path, second.log_path)
            assert results[0].log_path is not None
            self.assertEqual(results[0].log_path.read_text().count("cpm passed"), 1)

    async def test_verify_many_stops_after_shared_environment_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp), include_foyr=True)
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root, include_foyr=True)),
                    FakeResponse(
                        1,
                        'plugin type="dnsname" failed (add): cni plugin '
                        "dnsname failed: permission denied",
                    ),
                ]
            )
            results = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).verify_many(root / "workspace", ["cpm", "foyr"])

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].repository, "cpm")
            self.assertEqual(results[0].status, "environment_blocked")
            self.assertEqual(len(factory.calls), 2)

    async def test_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp), max_output_bytes=1024)
            factory = FakeProcessFactory(
                [
                    FakeResponse(0, rendered_config(root)),
                    FakeResponse(0, "startup:" + "s" * 5000),
                    FakeResponse(
                        0,
                        "verification-start:" + "x" * 5000 + ":verification-tail",
                    ),
                ]
            )

            result = await RuntimeManager(
                config, environ={}, process_factory=factory
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "passed")
            self.assertIn("[output truncated]", result.output)
            self.assertIn("verification-start:", result.output)
            self.assertIn(":verification-tail", result.output)
            self.assertLessEqual(
                len(result.output.encode("utf-8", errors="replace")),
                1024,
            )
            assert result.log_path is not None
            self.assertLessEqual(result.log_path.stat().st_size, 1024)
            retained_log = result.log_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            self.assertIn("[earlier runtime log entries truncated]", retained_log)
            self.assertIn("[retained runtime log entry truncated", retained_log)
            self.assertIn("pytest", retained_log)
            self.assertIn(":verification-tail", retained_log)

    async def test_symlinked_runtime_log_directory_is_rejected_before_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            outside = root / "outside"
            outside.mkdir()
            symphony = root / "workspace/.symphony"
            symphony.mkdir()
            (symphony / "runtime").symlink_to(
                outside,
                target_is_directory=True,
            )
            factory = FakeProcessFactory([])

            result = await RuntimeManager(
                config,
                environ={},
                process_factory=factory,
            ).verify(root / "workspace", "cpm")

            self.assertEqual(result.status, "environment_blocked")
            self.assertIn("safely open", result.message)
            self.assertEqual(factory.calls, [])
            self.assertEqual(list(outside.iterdir()), [])

    async def test_symlinked_runtime_artifact_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_directory = root / ".symphony/runtime"
            runtime_directory.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text("unchanged\n", encoding="utf-8")
            artifact = runtime_directory / "verification.json"
            artifact.symlink_to(outside)

            with self.assertRaises(RuntimeArtifactError):
                write_runtime_artifact_bytes(artifact, b"replacement\n")

            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")

    async def test_timeout_and_cancellation_terminate_injected_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = make_runtime(Path(tmp))
            hanging = HangingProcess()

            async def factory(*argv: str, **kwargs: Any) -> HangingProcess:
                return hanging

            manager = RuntimeManager(config, environ={}, process_factory=factory)
            timed_out = await manager._execute(["fake"], {}, 0.01)
            self.assertTrue(timed_out.timed_out)
            self.assertTrue(hanging.terminated)

            cancelled_process = HangingProcess()

            async def cancellation_factory(
                *argv: str, **kwargs: Any
            ) -> HangingProcess:
                return cancelled_process

            cancelled_manager = RuntimeManager(
                config, environ={}, process_factory=cancellation_factory
            )
            task = asyncio.create_task(
                cancelled_manager._execute(["fake"], {}, 60)
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled_process.terminated)

    async def test_exited_process_with_inherited_stdout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_runtime(Path(tmp), max_output_bytes=256)
            config.termination_grace_seconds = 0.01
            process = ExitedWithOpenStdoutProcess(
                b"complete-" + (b"\xff" * 512) + b"-tail"
            )

            async def factory(
                *argv: str, **kwargs: Any
            ) -> ExitedWithOpenStdoutProcess:
                return process

            fallback_eof_sent = False

            def send_fallback_eof() -> None:
                nonlocal fallback_eof_sent
                fallback_eof_sent = True
                process.stdout.feed_eof()

            fallback = asyncio.get_running_loop().call_later(
                0.1, send_fallback_eof
            )
            try:
                result = await asyncio.wait_for(
                    RuntimeManager(
                        config, environ={}, process_factory=factory
                    )._execute(["fake"], {}, 60),
                    0.5,
                )
            finally:
                fallback.cancel()
                if not process.stdout.at_eof():
                    process.stdout.feed_eof()

            self.assertEqual(result.returncode, 0)
            self.assertFalse(result.timed_out)
            self.assertIn("complete", result.output)
            self.assertIn("-tail", result.output)
            self.assertIn("[output truncated]", result.output)
            self.assertIn("?", result.output)
            self.assertIn("stdout remained open", result.output)
            self.assertLessEqual(len(result.output.encode("utf-8")), 256)
            self.assertFalse(process.terminated)
            self.assertFalse(process.killed)
            self.assertFalse(fallback_eof_sent)

    async def test_cancellation_does_not_wait_for_inherited_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_runtime(Path(tmp))
            process = HangingProcessWithOpenStdout()

            async def factory(
                *argv: str, **kwargs: Any
            ) -> HangingProcessWithOpenStdout:
                return process

            fallback_eof_sent = False

            def send_fallback_eof() -> None:
                nonlocal fallback_eof_sent
                fallback_eof_sent = True
                process.stdout.feed_eof()

            fallback = asyncio.get_running_loop().call_later(
                0.1, send_fallback_eof
            )
            task = asyncio.create_task(
                RuntimeManager(
                    config, environ={}, process_factory=factory
                )._execute(["fake"], {}, 60)
            )
            await asyncio.sleep(0)
            task.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.5)
            finally:
                fallback.cancel()
                if not process.stdout.at_eof():
                    process.stdout.feed_eof()

            self.assertTrue(process.terminated)
            self.assertFalse(process.killed)
            self.assertFalse(fallback_eof_sent)


def make_runtime(
    root: Path,
    *,
    include_foyr: bool = False,
    max_output_bytes: int = 1024 * 1024,
) -> tuple[Path, RuntimeConfig]:
    runtime_dir = root / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "compose.yml").write_text("services: {}\n")
    (runtime_dir / ".env").write_text("UNCHANGED=1\n")
    workspace = root / "workspace"
    cpm = workspace / "cpm"
    (cpm / ".git").mkdir(parents=True)
    (cpm / ".git/HEAD").write_text("ref: refs/heads/feature/T-1\n")
    repositories: dict[str, dict[str, Any]] = {
        "cpm": {
            "workspace_subdir": "cpm",
            "source_env": "CPM_SRC",
            "service": "cpm",
            "mount_target": "/src",
            "dependencies": ["db", "cache", "ibis"],
            "container_workdir": "/src",
            "verification_profile": "pytest",
        }
    }
    if include_foyr:
        foyr = workspace / "foyr"
        (foyr / ".git").mkdir(parents=True)
        (foyr / ".git/HEAD").write_text("ref: refs/heads/feature/T-1\n")
        repositories["foyr"] = {
            "workspace_subdir": "foyr",
            "source_env": "FOYR_SRC",
            "service": "foyr",
            "mount_target": "/app",
            "verification_profile": "pytest",
        }
    config = RuntimeConfig(
        enabled=True,
        command=["fake-podman", "compose"],
        project_directory=runtime_dir,
        compose_file=runtime_dir / "compose.yml",
        env_file=runtime_dir / ".env",
        project_name="shared",
        lock_file=root / "state/runtime.lock",
        max_output_bytes=max_output_bytes,
        repositories=repositories,
        verification_profiles={
            "pytest": {
                "argv": ["pytest", "-q"],
                "default_args": ["tests"],
                "environment": {"APP_CONFIG": "test.yml"},
                "timeout_seconds": 2,
            }
        },
    )
    return root, config


def rendered_config(root: Path, *, include_foyr: bool = False) -> str:
    services: dict[str, Any] = {
        "cpm": {
            "environment": {
                "DATABASE_PASSWORD": COMPOSE_SECRET,
                "DATABASE_DSN": (
                    f"oracle://user:{COMPOSE_SECRET}@database/service"
                ),
            },
            "volumes": [
                {
                    "type": "bind",
                    "source": str(root / "workspace/cpm"),
                    "target": "/src",
                }
            ]
        }
    }
    if include_foyr:
        services["foyr"] = {
            "volumes": [
                {
                    "type": "bind",
                    "source": str(root / "workspace/foyr"),
                    "target": "/app",
                }
            ]
        }
    return json.dumps({"services": services})


def shutdown_rendered_config(
    root: Path,
    *,
    include_foyr: bool = False,
    cpm_depends_on: Any = None,
) -> str:
    services = json.loads(
        rendered_config(root, include_foyr=include_foyr)
    )["services"]
    services["cpm"]["depends_on"] = (
        ["ibis"] if cpm_depends_on is None else cpm_depends_on
    )
    services.update(
        {
            "cache": {},
            "ibis": {
                "depends_on": {
                    "db": {
                        "condition": "service_started",
                    }
                }
            },
            "db": {},
        }
    )
    return json.dumps({"services": services})


if __name__ == "__main__":
    unittest.main()
