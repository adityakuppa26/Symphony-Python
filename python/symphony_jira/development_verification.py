from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .human_review import (
    HumanReviewContextError, capture_workspace_diff, read_frozen_text_artifact,
    write_frozen_text_artifact,
)
from .plan_spec import PlanSpec


class DevelopmentVerificationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: Literal["cpm", "foyr2", "pi"]
    suite: Literal["python", "karma"] = "python"
    reason: str = ""
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
        non_execution_flags = {"--collect-only", "--co", "--help", "-h", "--version", "--fixtures", "--fixtures-per-test"}
        if any(argument.split("=", 1)[0] in non_execution_flags for argument in value):
            raise ValueError("verification must execute tests, not only collect or describe them")
        return value

    @model_validator(mode="after")
    def supported_suite(self):
        if self.suite == "karma":
            if self.repository != "foyr2" or len(self.test_args) != 1:
                raise ValueError("karma requires foyr2 and one relative test selector")
            selector = self.test_args[0]
            if Path(selector).is_absolute() or ".." in Path(selector).parts or selector.startswith("-"):
                raise ValueError("karma selector must stay within foyr/web/karma_test")
        elif self.repository in {"cpm", "foyr2"}:
            root = "Test/unit" if self.repository == "cpm" else "tests"
            selectors = []
            option_value = False
            for argument in self.test_args:
                if option_value:
                    option_value = False
                    continue
                if argument in {"-k", "-m", "--maxfail", "--tb", "-r"}:
                    option_value = True
                elif argument in {"-q", "-qq", "-v", "-vv", "-x", "-s", "--disable-warnings"}:
                    continue
                elif argument.startswith(("--maxfail=", "--tb=")):
                    continue
                elif argument.startswith("-"):
                    raise ValueError(f"unsupported focused pytest option: {argument}")
                else:
                    path = Path(argument.split("::", 1)[0])
                    if path.is_absolute() or ".." in path.parts or not path.as_posix().startswith(root + "/"):
                        raise ValueError(f"{self.repository} requires focused selectors under {root}/")
                    selectors.append(argument)
            if option_value or not selectors:
                raise ValueError("focused pytest requests need a test path and complete option values")
        return self


class SkippedVerificationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: Literal["cpm", "foyr2", "pi"]
    reason: str = Field(min_length=1)


class DevelopmentVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    targets: list[DevelopmentVerificationTarget] = Field(
        min_length=0,
        max_length=6,
    )
    skipped: list[SkippedVerificationTarget] = Field(default_factory=list, max_length=3)
    workspace_diff_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_repositories: list[Literal["cpm", "foyr2", "pi"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_repositories(self) -> "DevelopmentVerificationRequest":
        repositories = [(target.repository, target.suite) for target in self.targets]
        if len(repositories) != len(set(repositories)):
            raise ValueError(
                "development verification repositories must be unique"
            )
        if self.workspace_diff_hash and not self.snapshot_repositories:
            raise ValueError("a code-bound request must name its snapshot repositories")
        if self.workspace_diff_hash and not {target.repository for target in self.targets} <= set(self.snapshot_repositories):
            raise ValueError("requested tests are outside the code snapshot")
        return self

    def validate_repositories(
        self,
        expected_repositories: tuple[str, ...],
    ) -> None:
        actual = {target.repository for target in self.targets} | {target.repository for target in self.skipped}
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
    if target.suite == "karma":
        return ["bash", "/symphony-runtime/foyr-frontend-tests.sh", target.test_args[0]]
    if target.repository == "cpm":
        return ["pytest", *target.test_args]
    if target.repository == "foyr2":
        return [
            "pytest",
            *target.test_args,
            "-n",
            "1",
            "--tb=native",
            "--junitxml=/tmp/symphony-foyr-pytest-results.xml",
        ]
    return ["hatch", "run", "dev:pytest", *target.test_args]


def execute_request(
    request: DevelopmentVerificationRequest,
    *,
    workspace_path: Path,
    entrypoint: Path,
    runtime_entrypoint: Path | None = None,
    result_path: str | None = None,
    plan_path: str = ".symphony/codex-plan.md",
    timeout_seconds: int = 1800,
    prepare_with_test: bool = False,
) -> int:
    results = []
    failures = []
    prepared: dict[str, int] = {}
    deadline = time.monotonic() + timeout_seconds
    deadline_epoch = int(time.time() + timeout_seconds)

    def code_matches() -> bool:
        if request.workspace_diff_hash is None:
            return True
        content = read_frozen_text_artifact(workspace_path, plan_path, label="verification PlanSpec", required=True)
        plan = PlanSpec.model_validate_json(content)
        snapshot = capture_workspace_diff(workspace_path, plan,
            managed_repositories=tuple(request.snapshot_repositories))
        return snapshot.content_hash == request.workspace_diff_hash

    def invoke(command):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("Verification time budget exhausted", flush=True)
            return 125
        process = None
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=os.name == "posix",
                env={**os.environ, "SYMPHONY_VERIFICATION_DEADLINE_EPOCH": str(deadline_epoch)})
            return process.wait(timeout=remaining)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Verification runner unavailable or timed out: {exc}", flush=True)
            if process is not None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait()
                except ProcessLookupError:
                    process.wait()
            return 125

    try:
        unchanged = code_matches()
    except (HumanReviewContextError, ValueError) as exc:
        print(f"Cannot validate verification code: {exc}", flush=True)
        unchanged = False
    for target in request.targets:
        if not unchanged:
            code = 125
        else:
            if runtime_entrypoint and not prepare_with_test and target.suite == "python" and target.repository not in prepared:
                prepared[target.repository] = invoke([
                    str(runtime_entrypoint), "up", str(workspace_path), target.repository,
                ])
            code = prepared.get(target.repository, 0) if target.suite == "python" else 0
            if code == 0:
                command = [str(entrypoint), target.repository, str(workspace_path)]
                if runtime_entrypoint and not prepare_with_test:
                    command.append("--test-only")
                code = invoke([*command, "--", *fixed_test_command(target)])
        state = "passed" if code == 0 else "environment_error" if code == 125 else "failed"
        results.append({"repository": target.repository, "suite": target.suite,
                        "test_args": target.test_args, "reason": target.reason,
                        "status": state, "returncode": code})
        if code:
            failures.append(code)
    try:
        unchanged = unchanged and code_matches()
    except (HumanReviewContextError, ValueError):
        unchanged = False
    status = ("stale" if not unchanged else "environment_error" if 125 in failures
              else "failed" if failures else "not_run" if not results
              else "partial" if request.skipped else "passed")
    report = {"status": status, "workspace_diff_hash": request.workspace_diff_hash,
              "finished_at": datetime.now(timezone.utc).isoformat(), "results": results,
              "skipped": [target.model_dump() for target in request.skipped]}
    if result_path:
        write_frozen_text_artifact(workspace_path, result_path, json.dumps(report, indent=2),
                                  label="verification result")
    print("Verification summary: " + json.dumps(report), flush=True)
    if status == "stale":
        return 125
    return failures[0] if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute a validated Codex development-verification request"
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--runtime-entrypoint")
    parser.add_argument("--prepare-with-test", action="store_true",
                        help="prepare and execute in one entrypoint call under its runtime lock")
    parser.add_argument("--result")
    parser.add_argument("--plan", default=".symphony/codex-plan.md")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
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
        runtime_entrypoint=Path(args.runtime_entrypoint).expanduser().resolve() if args.runtime_entrypoint else None,
        result_path=args.result,
        plan_path=args.plan,
        timeout_seconds=args.timeout_seconds,
        prepare_with_test=args.prepare_with_test,
    )


if __name__ == "__main__":
    raise SystemExit(main())
