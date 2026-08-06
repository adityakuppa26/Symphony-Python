from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from symphony_jira.config import (
    AutomationConfig,
    CodexConfig,
    ConfigError,
    JiraRequirementsConfig,
    RuntimeConfig,
    RuntimeVerificationProfileConfig,
    WorkflowConfig,
    load_config,
    validate_preflight,
)
from symphony_jira.workflow import load_workflow


class ConfigTests(unittest.TestCase):
    def test_automation_is_backwards_compatible_and_disabled_by_default(self) -> None:
        automation = AutomationConfig()

        self.assertFalse(automation.enabled)
        self.assertEqual(automation.workspace_subdir, Path("automation"))
        self.assertEqual(
            automation.output_plan_file,
            ".symphony/codex-automation-plan.md",
        )
        self.assertEqual(
            automation.output_result_file,
            ".symphony/codex-automation-final.md",
        )
        self.assertIn("no-op", automation.planning_prompt)
        self.assertIn("no-op", automation.implementation_prompt)
        self.assertFalse(WorkflowConfig().automation.enabled)

    def test_automation_paths_must_be_safe_workspace_relative_paths(self) -> None:
        for field_name in (
            "workspace_subdir",
            "output_plan_file",
            "output_result_file",
        ):
            for value in (
                "",
                " ",
                ".",
                "/tmp/result",
                "../result",
                "a/../result",
                "a\\result",
            ):
                with self.subTest(field_name=field_name, value=value):
                    with self.assertRaises(ValueError):
                        AutomationConfig(**{field_name: value})

    def test_automation_paths_are_normalized(self) -> None:
        automation = AutomationConfig(
            workspace_subdir=" ./checks/automation ",
            output_plan_file=" .symphony/./automation-plan.md ",
            output_result_file=" artifacts//automation-final.md ",
        )

        self.assertEqual(automation.workspace_subdir, Path("checks/automation"))
        self.assertEqual(
            automation.output_plan_file,
            ".symphony/automation-plan.md",
        )
        self.assertEqual(
            automation.output_result_file,
            "artifacts/automation-final.md",
        )

    def test_automation_prompts_must_be_non_blank(self) -> None:
        for field_name in ("planning_prompt", "implementation_prompt"):
            for value in ("", " ", "\x00"):
                with self.subTest(field_name=field_name, value=value):
                    with self.assertRaises(ValueError):
                        AutomationConfig(**{field_name: value})

    def test_enabled_automation_artifacts_must_use_a_separate_layout(self) -> None:
        with self.assertRaisesRegex(ValueError, "stored under .symphony"):
            WorkflowConfig(
                automation=AutomationConfig(
                    enabled=True,
                    output_result_file="artifacts/automation-final.md",
                )
            )

        with self.assertRaisesRegex(ValueError, "must not overlap"):
            WorkflowConfig(
                automation=AutomationConfig(
                    enabled=True,
                    output_plan_file=".symphony/automation.json",
                    output_result_file=".symphony/automation.json",
                )
            )
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            WorkflowConfig(
                automation=AutomationConfig(
                    enabled=True,
                    output_plan_file=".symphony/automation",
                    output_result_file=".symphony/automation/result.md",
                )
            )

        for field_name in ("output_plan_file", "output_result_file"):
            with self.subTest(artifact_inside_checkout=field_name):
                with self.assertRaisesRegex(
                    ValueError,
                    "must be outside the automation checkout",
                ):
                    WorkflowConfig(
                        automation=AutomationConfig(
                            enabled=True,
                            **{field_name: "automation/artifact.json"},
                        )
                    )

        for codex_field in (
            "output_last_message_file",
            "output_plan_file",
            "output_review_file",
            "output_review_history_file",
            "output_human_review_triage_file",
        ):
            with self.subTest(codex_collision=codex_field):
                with self.assertRaisesRegex(ValueError, "must not overlap"):
                    WorkflowConfig(
                        codex=CodexConfig(
                            **{
                                codex_field: (
                                    ".symphony/codex-automation-plan.md"
                                )
                            }
                        ),
                        automation=AutomationConfig(enabled=True),
                    )

        with self.assertRaisesRegex(
            ValueError,
            "codex.output_last_message_file must not overlap the automation checkout",
        ):
            WorkflowConfig(
                codex=CodexConfig(output_last_message_file="automation/final.md"),
                automation=AutomationConfig(enabled=True),
            )

    def test_enabled_automation_checkout_must_not_use_reserved_or_runtime_paths(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "reserved .symphony"):
            WorkflowConfig(
                automation=AutomationConfig(
                    enabled=True,
                    workspace_subdir=".symphony/automation",
                )
            )

        runtime_repository = {
            "source_env": "AUTOMATION_SRC",
            "service": "automation",
            "mount_target": "/automation",
            "verification_profile": "tests",
        }
        for runtime_subdir in ("automation", "automation/runtime", "."):
            with self.subTest(runtime_subdir=runtime_subdir):
                with self.assertRaisesRegex(ValueError, "must not overlap"):
                    WorkflowConfig(
                        automation=AutomationConfig(enabled=True),
                        runtime=RuntimeConfig(
                            repositories={
                                "automation": {
                                    **runtime_repository,
                                    "workspace_subdir": runtime_subdir,
                                }
                            }
                        ),
                    )

    def test_jira_attachments_are_disabled_by_default(self) -> None:
        requirements = JiraRequirementsConfig()

        self.assertFalse(requirements.download_attachments)
        self.assertFalse(requirements.require_attachment_analysis)

    def test_default_planning_prompt_uses_existing_behavior_for_incidental_edges(self) -> None:
        prompt = CodexConfig().planning_prompt

        self.assertIn("incidental edge cases", prompt)
        self.assertIn("established repository behavior", prompt)
        self.assertIn("no applicable precedent exists", prompt)

    def test_checked_in_workflow_enables_post_handoff_runtime_shutdown(self) -> None:
        workflow_path = Path(__file__).resolve().parents[1] / "WORKFLOW.md"

        workflow = load_workflow(workflow_path, environ={})

        self.assertTrue(workflow.config.automation.enabled)
        self.assertEqual(
            workflow.config.automation.workspace_subdir,
            Path("automation"),
        )
        self.assertIn(
            "git clone --no-hardlinks --branch master --single-branch /home/adkuppa/CPM automation",
            workflow.config.hooks.after_create or "",
        )
        self.assertIn(
            "git -C automation remote remove origin",
            workflow.config.hooks.after_create or "",
        )
        self.assertIn(
            'git -C automation checkout -b "{{ issue.identifier }}"',
            workflow.config.hooks.after_create or "",
        )
        self.assertIn(
            "if [ ! -e automation ]; then",
            workflow.config.hooks.before_run or "",
        )
        self.assertIn(
            "git clone --no-hardlinks --branch master --single-branch /home/adkuppa/CPM automation",
            workflow.config.hooks.before_run or "",
        )
        self.assertIn(
            "git -C automation remote remove origin",
            workflow.config.hooks.before_run or "",
        )
        self.assertIn(
            'test -z "$(git -C automation remote)"',
            workflow.config.hooks.before_run or "",
        )
        self.assertIn(
            'git -C automation checkout -b "{{ issue.identifier }}"',
            workflow.config.hooks.before_run or "",
        )
        self.assertIn(
            'test "$(git -C automation symbolic-ref --short HEAD)" = "{{ issue.identifier }}"',
            workflow.config.hooks.before_run or "",
        )
        self.assertIn(
            "git -C automation show-ref --verify --quiet refs/heads/master",
            workflow.config.hooks.before_run or "",
        )
        self.assertIn(
            "git -C automation status --short",
            workflow.config.hooks.before_run or "",
        )
        self.assertIn(
            "git -C automation diff --check",
            workflow.config.hooks.verify or "",
        )
        self.assertTrue(workflow.config.runtime.shutdown_after_handoff)
        self.assertEqual(workflow.config.runtime.shutdown_grace_seconds, 120)
        foyr = workflow.config.runtime.repositories["foyr2"]
        self.assertEqual(foyr.force_recreate_dependencies, ["ibis"])
        cpm = workflow.config.runtime.repositories["cpm"]
        self.assertEqual(
            cpm.dependencies,
            ["oracledb19", "memcached"],
        )
        cpm_profile = workflow.config.runtime.verification_profiles["cpm_pytest"]
        self.assertEqual(
            cpm_profile.argv,
            ["pytest"],
        )
        self.assertEqual(
            cpm_profile.default_args,
            ["Test/unit"],
        )
        self.assertEqual(cpm_profile.environment, {})

    def test_runtime_is_backwards_compatible_and_disabled_by_default(self) -> None:
        config = RuntimeConfig()

        self.assertFalse(config.enabled)
        self.assertFalse(config.required)
        self.assertFalse(config.shutdown_after_handoff)
        self.assertEqual(config.shutdown_grace_seconds, 120)

    def test_runtime_shutdown_grace_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig(shutdown_grace_seconds=0)

    def test_runtime_output_limit_must_fit_retained_evidence_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be at least 256"):
            RuntimeConfig(max_output_bytes=255)
        with self.assertRaisesRegex(ValueError, "must be at most 16777216"):
            RuntimeConfig(max_output_bytes=16 * 1024 * 1024 + 1)

    def test_enabled_runtime_defaults_to_required_and_resolves_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                {
                    "runtime": {
                        "enabled": True,
                        "shutdown_after_handoff": True,
                        "command": ["./bin/podman", "compose"],
                        "project_directory": "./runtime",
                        "compose_file": "./runtime/compose.yml",
                        "env_file": "./runtime/.env",
                        "project_name": "shared",
                        "lock_file": "./state/runtime.lock",
                        "repositories": {
                            "cpm": {
                                "workspace_subdir": "cpm",
                                "source_env": "CPM_SRC",
                                "service": "cpm",
                                "mount_target": "/src",
                                "verification_profile": "pytest",
                            }
                        },
                        "verification_profiles": {
                            "pytest": {
                                "argv": ["pytest"],
                                "default_args": ["tests"],
                                "environment": {"APP_CONFIG": "test.yml"},
                            }
                        },
                    }
                },
                root / "WORKFLOW.md",
                environ={},
            )

        self.assertTrue(config.runtime.required)
        self.assertTrue(config.runtime.shutdown_after_handoff)
        self.assertEqual(config.runtime.project_directory, root / "runtime")
        self.assertEqual(config.runtime.compose_file, root / "runtime/compose.yml")
        self.assertEqual(config.runtime.env_file, root / "runtime/.env")
        self.assertEqual(config.runtime.lock_file, root / "state/runtime.lock")
        self.assertEqual(config.runtime.command[0], str(root / "bin/podman"))

    def test_runtime_rejects_unknown_profile_and_unsafe_workspace_path(self) -> None:
        base = {
            "enabled": True,
            "project_directory": "/runtime",
            "compose_file": "/runtime/compose.yml",
            "env_file": "/runtime/.env",
            "project_name": "shared",
            "lock_file": "/tmp/runtime.lock",
            "verification_profiles": {"pytest": {"argv": ["pytest"]}},
        }
        with self.assertRaises(ConfigError):
            load_config(
                {
                    "runtime": {
                        **base,
                        "repositories": {
                            "cpm": {
                                "workspace_subdir": "cpm",
                                "source_env": "CPM_SRC",
                                "service": "cpm",
                                "mount_target": "/src",
                                "verification_profile": "missing",
                            }
                        },
                    }
                },
                Path("/tmp/WORKFLOW.md"),
                environ={},
            )
        with self.assertRaises(ConfigError):
            load_config(
                {
                    "runtime": {
                        **base,
                        "repositories": {
                            "cpm": {
                                "workspace_subdir": "../cpm",
                                "source_env": "CPM_SRC",
                                "service": "cpm",
                                "mount_target": "/src",
                                "verification_profile": "pytest",
                            }
                        },
                    }
                },
                Path("/tmp/WORKFLOW.md"),
                environ={},
            )

    def test_runtime_rejects_required_when_disabled(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig(required=True)

    def test_runtime_allows_workspace_root_repository(self) -> None:
        config = RuntimeConfig(
            **runtime_config_values(
                Path("/tmp"),
                command=["podman", "compose"],
                project_directory=Path("/runtime"),
                compose_file=Path("/runtime/compose.yml"),
                env_file=Path("/runtime/.env"),
            )
        )
        config.repositories["cpm"].workspace_subdir = Path(".")
        reparsed = RuntimeConfig.model_validate(config.model_dump())

        self.assertEqual(reparsed.repositories["cpm"].workspace_subdir, Path("."))
        self.assertEqual(reparsed.repository_key_for_workspace_subdir("."), "cpm")

    def test_runtime_maps_normalized_workspace_subdir_to_repository_key(self) -> None:
        values = runtime_config_values(
            Path("/tmp"),
            command=["podman", "compose"],
            project_directory=Path("/runtime"),
            compose_file=Path("/runtime/compose.yml"),
            env_file=Path("/runtime/.env"),
        )
        repositories = values["repositories"]
        assert isinstance(repositories, dict)
        repositories["backend"] = repositories.pop("cpm")
        repositories["backend"]["workspace_subdir"] = "services/./api"

        config = RuntimeConfig(**values)

        self.assertEqual(
            config.repositories["backend"].workspace_subdir,
            Path("services/api"),
        )
        self.assertEqual(
            config.repository_keys_by_workspace_subdir(),
            {"services/api": "backend"},
        )
        self.assertEqual(
            config.repository_key_for_workspace_subdir("services/api"),
            "backend",
        )

    def test_runtime_rejects_duplicate_normalized_workspace_subdirs(self) -> None:
        values = runtime_config_values(
            Path("/tmp"),
            command=["podman", "compose"],
            project_directory=Path("/runtime"),
            compose_file=Path("/runtime/compose.yml"),
            env_file=Path("/runtime/.env"),
        )
        repositories = values["repositories"]
        assert isinstance(repositories, dict)
        repositories["cpm"]["workspace_subdir"] = "services/api"
        repositories["backend"] = {
            "workspace_subdir": "services/./api",
            "source_env": "BACKEND_SRC",
            "service": "backend",
            "mount_target": "/backend",
            "verification_profile": "pytest",
        }

        with self.assertRaisesRegex(
            ValueError,
            "unique normalized workspace_subdir values",
        ):
            RuntimeConfig(**values)

    def test_runtime_force_recreate_dependencies_must_be_declared(self) -> None:
        values = runtime_config_values(
            Path("/tmp"),
            command=["podman", "compose"],
            project_directory=Path("/runtime"),
            compose_file=Path("/runtime/compose.yml"),
            env_file=Path("/runtime/.env"),
        )
        repository = values["repositories"]["cpm"]
        repository["dependencies"] = ["db"]
        repository["force_recreate_dependencies"] = ["ibis"]

        with self.assertRaisesRegex(
            ValueError,
            "force_recreate_dependencies must be listed in dependencies: ibis",
        ):
            RuntimeConfig(**values)

    def test_preflight_reports_missing_runtime_inputs_without_invoking_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text("Prompt\n")
            config = WorkflowConfig(
                runtime=runtime_config_values(
                    root,
                    command=[str(root / "missing-podman")],
                    project_directory=root / "missing-project",
                    compose_file=root / "missing-compose.yml",
                    env_file=root / "missing.env",
                )
            )

            issues = validate_preflight(
                workflow_path,
                config,
                environ={},
                check_jira_credentials=False,
                check_codex=False,
            )

        runtime_codes = {
            issue.code for issue in issues if issue.code.startswith("runtime_")
        }
        self.assertEqual(
            runtime_codes,
            {
                "runtime_command_missing",
                "runtime_provider_missing",
                "runtime_project_directory",
                "runtime_compose_file",
                "runtime_env_file",
            },
        )

    def test_preflight_accepts_existing_runtime_inputs_without_running_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text("Prompt\n")
            project = root / "runtime"
            project.mkdir()
            compose_file = project / "compose.yml"
            compose_file.write_text("services: {}\n")
            env_file = project / ".env"
            env_file.write_text("")
            provider = root / "podman"
            provider.write_text("#!/bin/sh\nexit 99\n")
            provider.chmod(0o700)
            config = WorkflowConfig(
                runtime=runtime_config_values(
                    root,
                    command=[str(provider), "compose"],
                    project_directory=project,
                    compose_file=compose_file,
                    env_file=env_file,
                )
            )

            issues = validate_preflight(
                workflow_path,
                config,
                environ={},
                check_jira_credentials=False,
                check_codex=False,
            )

        self.assertFalse(
            [issue for issue in issues if issue.code.startswith("runtime_")]
        )

    def test_codex_environment_exclusions_have_safe_defaults_and_are_configurable(self) -> None:
        self.assertEqual(
            CodexConfig().environment_exclude,
            ["JIRA_*", "*_JIRA_TOKEN", "*_JIRA_EMAIL"],
        )
        self.assertEqual(
            CodexConfig(environment_exclude=["PRIVATE_*", "EXACT_NAME"]).environment_exclude,
            ["PRIVATE_*", "EXACT_NAME"],
        )

    def test_codex_environment_exclusions_reject_blank_patterns(self) -> None:
        with self.assertRaises(ValueError):
            CodexConfig(environment_exclude=["JIRA_*", " "])

    def test_validate_accepts_fake_codex_and_jira_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
  jql: "project = T"
workspace:
  root: "./workspaces"
  strategy: hook_only
codex:
  command: "{fake_codex}"
---
Prompt
""",
                encoding="utf-8",
            )
            env = {"TEST_JIRA_TOKEN": "token"}
            workflow = load_workflow(workflow_path, environ=env)

            issues = validate_preflight(workflow_path, workflow.config, environ=env)

        self.assertEqual(issues, [])

    def test_validate_catches_missing_jira_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
  jql: "project = T"
workspace:
  strategy: hook_only
codex:
  command: "{fake_codex}"
---
Prompt
""",
                encoding="utf-8",
            )
            workflow = load_workflow(workflow_path, environ={})

            issues = validate_preflight(workflow_path, workflow.config, environ={})

        self.assertIn("jira_token_missing", {issue.code for issue in issues})

    def test_validate_accepts_jira_token_from_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            token_file = root / "config.toml"
            token_file.write_text('JIRA_PERSONAL_TOKEN = "token-from-file"\n', encoding="utf-8")
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
    token_config_file: "{token_file}"
    token_config_key: JIRA_PERSONAL_TOKEN
  jql: "project = T"
workspace:
  strategy: hook_only
codex:
  command: "{fake_codex}"
---
Prompt
""",
                encoding="utf-8",
            )
            workflow = load_workflow(workflow_path, environ={})

            issues = validate_preflight(workflow_path, workflow.config, environ={})

        self.assertEqual(issues, [])

    def test_validate_catches_missing_git_worktree_source_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
  jql: "project = T"
workspace:
  strategy: git_worktree
  source_repo: "./missing"
codex:
  command: "{fake_codex}"
---
Prompt
""",
                encoding="utf-8",
            )
            env = {"TEST_JIRA_TOKEN": "token"}
            workflow = load_workflow(workflow_path, environ=env)

            issues = validate_preflight(workflow_path, workflow.config, environ=env)

        self.assertIn("source_repo_missing", {issue.code for issue in issues})


def write_fake_codex(root: Path) -> Path:
    path = root / "fake_codex.py"
    path.write_text(
        """#!/usr/bin/env python3
import sys
if "--version" in sys.argv:
    print("fake codex 0.0")
    sys.exit(0)
print("{}")
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def runtime_config_values(
    root: Path,
    *,
    command: list[str],
    project_directory: Path,
    compose_file: Path,
    env_file: Path,
) -> dict[str, object]:
    return {
        "enabled": True,
        "command": command,
        "project_directory": project_directory,
        "compose_file": compose_file,
        "env_file": env_file,
        "project_name": "shared",
        "lock_file": root / "runtime.lock",
        "repositories": {
            "cpm": {
                "workspace_subdir": "cpm",
                "source_env": "CPM_SRC",
                "service": "cpm",
                "mount_target": "/src",
                "verification_profile": "pytest",
            }
        },
        "verification_profiles": {"pytest": {"argv": ["pytest"]}},
    }


if __name__ == "__main__":
    unittest.main()
