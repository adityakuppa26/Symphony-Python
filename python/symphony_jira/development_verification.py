from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .human_review import HumanReviewContextError, read_frozen_text_artifact


class DevelopmentVerificationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: Literal["cpm", "foyr2", "pi"]
    test_args: list[str] = Field(min_length=1, max_length=64)

    @field_validator("test_args")
    @classmethod
    def valid_test_arguments(cls, value: list[str]) -> list[str]:
        if any(
            not argument.strip()
            or "\x00" in argument
            or len(argument) > 1_024
            for argument in value
        ):
            raise ValueError(
                "development verification arguments must be non-empty, bounded, "
                "and contain no NUL"
            )
        return value


class DevelopmentVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    targets: list[DevelopmentVerificationTarget] = Field(
        min_length=1,
        max_length=3,
    )

    @model_validator(mode="after")
    def unique_repositories(self) -> "DevelopmentVerificationRequest":
        repositories = [target.repository for target in self.targets]
        if len(repositories) != len(set(repositories)):
            raise ValueError(
                "development verification repositories must be unique"
            )
        return self

    def validate_repositories(
        self,
        expected_repositories: tuple[str, ...],
    ) -> None:
        actual = {target.repository for target in self.targets}
        expected = set(expected_repositories)
        if actual != expected:
            raise ValueError(
                "development verification request must cover exactly the approved "
                f"PlanSpec repositories; expected {sorted(expected)}, got "
                f"{sorted(actual)}"
            )


def read_development_verification_request(
    workspace_path: Path,
    relative_path: str,
) -> tuple[DevelopmentVerificationRequest, str]:
    content = read_frozen_text_artifact(
        workspace_path,
        relative_path,
        label="development verification request",
        required=True,
    )
    if not content:
        raise HumanReviewContextError(
            "development verification request is missing or empty"
        )
    request = DevelopmentVerificationRequest.model_validate_json(content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return request, digest


def fixed_test_command(target: DevelopmentVerificationTarget) -> list[str]:
    if target.repository == "cpm":
        return ["pytest", *target.test_args]
    if target.repository == "foyr2":
        return [
            "pytest",
            *target.test_args,
            "-n",
            "4",
            "--tb=native",
            "--junitxml=/tmp/symphony-foyr-pytest-results.xml",
        ]
    return ["hatch", "run", "dev:pytest", *target.test_args]


def execute_request(
    request: DevelopmentVerificationRequest,
    *,
    workspace_path: Path,
    entrypoint: Path,
) -> int:
    for target in request.targets:
        completed = subprocess.run(
            [
                str(entrypoint),
                target.repository,
                str(workspace_path),
                "--",
                *fixed_test_command(target),
            ],
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute a validated Codex development-verification request"
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--entrypoint", required=True)
    args = parser.parse_args(argv)

    workspace_path = Path(args.workspace).expanduser().resolve()
    request, digest = read_development_verification_request(
        workspace_path,
        args.request,
    )
    if digest != args.sha256:
        raise SystemExit(
            "development verification request changed after Symphony validation"
        )
    return execute_request(
        request,
        workspace_path=workspace_path,
        entrypoint=Path(args.entrypoint).expanduser().resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
