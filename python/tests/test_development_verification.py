from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from symphony_jira.development_verification import (
    DevelopmentVerificationRequest,
    execute_request,
    fixed_test_command,
    read_development_verification_request,
)


def test_request_is_strict_and_binds_exact_plan_repositories() -> None:
    request = DevelopmentVerificationRequest.model_validate(
        {
            "schema_version": "1.0",
            "targets": [
                {
                    "repository": "cpm",
                    "test_args": ["Test/unit/test_example.py::test_behavior"],
                },
                {
                    "repository": "foyr2",
                    "test_args": ["tests/test_example.py::test_behavior"],
                },
            ],
        }
    )

    request.validate_repositories(("cpm", "foyr2"))
    with pytest.raises(ValueError, match="cover exactly"):
        request.validate_repositories(("cpm",))
    with pytest.raises(ValidationError):
        DevelopmentVerificationRequest.model_validate(
            {
                "schema_version": "1.0",
                "targets": [
                    {"repository": "cpm", "test_args": ["tests"]},
                    {"repository": "cpm", "test_args": ["other"]},
                ],
            }
        )


def test_fixed_commands_do_not_accept_a_model_authored_executable() -> None:
    request = DevelopmentVerificationRequest.model_validate(
        {
            "schema_version": "1.0",
            "targets": [
                {
                    "repository": "cpm",
                    "test_args": ["Test/unit/test_example.py", "-k", "focused"],
                },
                {
                    "repository": "pi",
                    "test_args": ["src/test/unit/test_example.py"],
                },
            ],
        }
    )

    self_cpm, self_pi = request.targets
    assert fixed_test_command(self_cpm) == [
        "pytest",
        "Test/unit/test_example.py",
        "-k",
        "focused",
    ]
    assert fixed_test_command(self_pi) == [
        "hatch",
        "run",
        "dev:pytest",
        "src/test/unit/test_example.py",
    ]


def test_executor_passes_literal_arguments_to_the_trusted_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        capture = root / "argv.json"
        entrypoint = root / "fake-test.py"
        entrypoint.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        entrypoint.chmod(0o700)
        request = DevelopmentVerificationRequest.model_validate(
            {
                "schema_version": "1.0",
                "targets": [
                    {
                        "repository": "cpm",
                        "test_args": [
                            "Test/unit/test_example.py::test_behavior",
                            "-k",
                            "value with spaces;$(not-a-shell)",
                        ],
                    }
                ],
            }
        )

        status = execute_request(
            request,
            workspace_path=workspace,
            entrypoint=entrypoint,
        )

        assert status == 0
        assert json.loads(capture.read_text(encoding="utf-8")) == [
            "cpm",
            str(workspace),
            "--",
            "pytest",
            "Test/unit/test_example.py::test_behavior",
            "-k",
            "value with spaces;$(not-a-shell)",
        ]


def test_request_reader_hashes_the_exact_validated_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        request_path = workspace / ".symphony" / "request.json"
        request_path.parent.mkdir(mode=0o700)
        request_path.write_text(
            '{"schema_version":"1.0","targets":['
            '{"repository":"foyr2","test_args":["tests/test_x.py"]}]}',
            encoding="utf-8",
        )

        request, digest = read_development_verification_request(
            workspace,
            ".symphony/request.json",
        )

        assert request.targets[0].repository == "foyr2"
        assert len(digest) == 64


def test_python_and_frontend_suites_can_cover_one_changed_repository() -> None:
    request = DevelopmentVerificationRequest.model_validate({
        "schema_version": "1.0", "targets": [
            {"repository": "foyr2", "suite": "python", "test_args": ["tests/views/api/test_home.py"]},
            {"repository": "foyr2", "suite": "karma", "test_args": ["cpm/home/homeTest.js"]},
        ],
    })
    request.validate_repositories(("foyr2",))
    command = fixed_test_command(request.targets[1])
    assert command[-1] == "cpm/home/homeTest.js"
    assert command[:2] == ["bash", "/symphony-runtime/foyr-frontend-tests.sh"]
    with pytest.raises(ValueError):
        DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [
            {"repository": "cpm", "suite": "karma", "test_args": ["../outside.js"]},
        ]})


def test_executor_prepares_once_records_each_result_and_continues_after_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = root / "runtime.py"
        calls = root / "calls.jsonl"
        runtime.write_text("#!/usr/bin/env python3\nimport json,sys,pathlib\n"
            f"p=pathlib.Path({str(calls)!r})\n"
            "with p.open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
        runtime.chmod(0o700)
        entrypoint = root / "test.py"
        entrypoint.write_text("#!/usr/bin/env python3\nimport json,sys,pathlib\n"
            f"p=pathlib.Path({str(calls)!r})\n"
            "with p.open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "raise SystemExit(1 if sys.argv[1]=='cpm' else 0)\n")
        entrypoint.chmod(0o700)
        request = DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [
            {"repository": "cpm", "test_args": ["Test/unit/test_x.py"]},
            {"repository": "foyr2", "test_args": ["tests/test_y.py"]},
        ]})
        status = execute_request(request, workspace_path=root, entrypoint=entrypoint,
            runtime_entrypoint=runtime, result_path=".symphony/result.json")
        report = json.loads((root / ".symphony/result.json").read_text())
        assert status == 1
        assert report["status"] == "failed"
        assert [item["status"] for item in report["results"]] == ["failed", "passed"]
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        assert commands[0] == ["up", str(root), "cpm"]
        assert commands[1][:4] == ["cpm", str(root), "--test-only", "--"]
        assert commands[2] == ["up", str(root), "foyr2"]


def test_skipped_coverage_is_not_reported_as_passing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        request = DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [],
            "skipped": [{"repository": "foyr2", "reason": "No applicable frontend fixture exists."}]})
        request.validate_repositories(("foyr2",))
        execute_request(request, workspace_path=root, entrypoint=root / "must-not-run",
                        result_path=".symphony/result.json")
        report = json.loads((root / ".symphony/result.json").read_text())
        assert report["status"] == "not_run"
        assert report["results"] == []


def test_runtime_start_failure_is_an_environment_result() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = root / "runtime.sh"
        runtime.write_text("#!/bin/sh\nexit 125\n")
        runtime.chmod(0o700)
        request = DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [
            {"repository": "foyr2", "test_args": ["tests/test_x.py"]},
        ]})
        assert execute_request(request, workspace_path=root, entrypoint=root / "must-not-run",
            runtime_entrypoint=runtime, result_path=".symphony/result.json") == 125
        assert json.loads((root / ".symphony/result.json").read_text())["status"] == "environment_error"


def test_code_drift_rejects_test_execution_and_records_stale_result() -> None:
    from symphony_jira.human_review import capture_workspace_diff
    from symphony_jira.plan_spec import PlanSpec
    from test_orchestrator import ensure_test_git_repository, valid_plan_spec_message

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sha = ensure_test_git_repository(root, repository="foyr2")
        message = valid_plan_spec_message('requirements_snapshot_hash "' + 'a' * 64 + '"',
            "Implement scoped behavior", baseline_sha=sha, repository="foyr2")
        plan = PlanSpec.model_validate_json(message)
        (root / ".symphony").mkdir()
        (root / ".symphony/codex-plan.md").write_text(message)
        before = capture_workspace_diff(root, plan)
        request = DevelopmentVerificationRequest.model_validate({"schema_version": "1.0",
            "targets": [{"repository": "foyr2", "test_args": ["tests/test_x.py"]}],
            "workspace_diff_hash": before.content_hash, "snapshot_repositories": ["foyr2"]})
        (root / "foyr2/changed.py").write_text("changed after selection\n")
        assert execute_request(request, workspace_path=root, entrypoint=root / "must-not-run",
            result_path=".symphony/result.json") == 125
        assert json.loads((root / ".symphony/result.json").read_text())["status"] == "stale"


def test_collection_only_requests_cannot_claim_verification() -> None:
    with pytest.raises(ValueError, match="execute tests"):
        DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [
            {"repository": "foyr2", "test_args": ["tests/test_x.py", "--collect-only"]},
        ]})


def test_browser_target_does_not_require_backend_runtime(tmp_path):
    entrypoint = tmp_path / 'test.sh'
    entrypoint.write_text('#!/bin/sh\nexit 0\n')
    entrypoint.chmod(0o700)
    request = DevelopmentVerificationRequest.model_validate({
        'schema_version': '1.0',
        'targets': [{'repository': 'foyr2', 'suite': 'karma', 'test_args': ['cpm/home/homeTest.js']}],
    })
    assert execute_request(request, workspace_path=tmp_path, entrypoint=entrypoint,
                           runtime_entrypoint=tmp_path / 'unavailable-backend-runtime') == 0


def test_foyr_verification_uses_one_worker_for_the_shared_vm():
    request = DevelopmentVerificationRequest.model_validate({
        'schema_version': '1.0',
        'targets': [{'repository': 'foyr2', 'test_args': ['tests/test_home.py']}],
    })
    command = fixed_test_command(request.targets[0])
    assert command[command.index('-n') + 1] == '1'


@pytest.mark.parametrize('repository,args', [
    ('cpm',['api/tests/test_x.py']), ('cpm',['Test/functional/test_x.py']),
    ('cpm',['Test/unit']), ('foyr2',['foyr/client_src/test_x.py']),
    ('foyr2',['-k','home']), ('foyr2',['tests/test_home.py','--setup-only']),
    ('cpm',['Test/unit/../functional/test_x.py']),
])
def test_python_requests_are_focused_and_routed_to_supported_families(repository,args):
    with pytest.raises(ValidationError):
        DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[{'repository':repository,'test_args':args}]})


def test_prepare_with_test_uses_one_entrypoint_call(tmp_path):
    entrypoint=tmp_path/'test.sh'; capture=tmp_path/'args'
    entrypoint.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{capture}"\n')
    entrypoint.chmod(0o700)
    request=DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[{'repository':'cpm','test_args':['Test/unit/test_x.py']}]})
    assert execute_request(request, workspace_path=tmp_path, entrypoint=entrypoint,
                           runtime_entrypoint=tmp_path/'unused', prepare_with_test=True) == 0
    assert '--test-only' not in capture.read_text()
    assert 'Test/unit/test_x.py' in capture.read_text()
