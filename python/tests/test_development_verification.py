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
