from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .human_review import (
    HumanReviewContextError, capture_workspace_diff, read_frozen_text_artifact,
    write_frozen_text_artifact,
)
from .plan_spec import PlanSpec


CPM_VERIFICATION_ENTRYPOINT = Path("/home/adkuppa/playground/cpm-verification/verify.py")
FOYR_VERIFICATION_ENTRYPOINT = Path("/home/adkuppa/playground/cpm-verification/foyr.py")


def validate_foyr_frontend_selector(selector: str) -> None:
    path = Path(selector)
    if path.is_absolute() or ".." in path.parts or any(c in selector for c in "*?[]{}"):
        raise ValueError("frontend requires explicit repository-relative test files")
    if (selector.startswith("foyr/client_src/karma_test/") and selector.lower().endswith("test.js")):
        return
    if selector.startswith("foyr/src/") and selector.endswith((".spec.ts", ".spec.tsx")):
        return
    raise ValueError("unsupported Foyr frontend test family")


class DevelopmentVerificationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: Literal["cpm", "foyr2", "pi"]
    suite: Literal["python", "functional", "frontend", "karma", "smoke"] = "python"
    reason: str = ""
    test_args: list[str] = Field(min_length=1, max_length=104)

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
        if self.suite == "functional" and self.repository != "cpm":
            raise ValueError("functional suite requires cpm")
        if self.suite == "smoke":
            profiles = {"foyr2": ["projects-page"]}
            if self.test_args != profiles.get(self.repository):
                raise ValueError("smoke requires foyr2/projects-page; CPM verification uses the schema stack")
        elif self.suite == "frontend":
            if self.repository != "foyr2":
                raise ValueError("frontend suite requires foyr2")
            for selector in self.test_args:
                validate_foyr_frontend_selector(selector)
        elif self.suite == "karma":
            if self.repository != "foyr2" or len(self.test_args) != 1:
                raise ValueError("karma requires foyr2 and one relative test selector")
            selector = self.test_args[0]
            if Path(selector).is_absolute() or ".." in Path(selector).parts or selector.startswith("-"):
                raise ValueError("karma selector must stay within foyr/web/karma_test")
        elif self.repository in {"cpm", "foyr2"}:
            root = ("Test/functional" if self.suite == "functional" else "Test/unit") if self.repository == "cpm" else "tests"
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
                    if (self.suite == "functional" or self.repository == "foyr2") and path.suffix != ".py":
                        raise ValueError("verification requires explicit test files or node IDs")
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
        max_length=10,
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


def include_changed_test_files(
    request: DevelopmentVerificationRequest, *, workspace_path: Path,
    repositories: tuple[str, ...], baseline_shas: dict[str, str],
    enabled_suites: list[str],
) -> DevelopmentVerificationRequest:
    """Run whole changed test files so new cases cannot be omitted by selection."""
    targets = [target.model_dump() for target in request.targets]
    skipped = [target.model_dump() for target in request.skipped]
    for repository in repositories:
        checkout = workspace_path / repository
        def git(*arguments: str) -> list[str]:
            try:
                result = subprocess.run(
                    ["git", "-C", str(checkout), *arguments], check=True,
                    capture_output=True, text=True, timeout=15,
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"Cannot discover changed tests for {repository}: {exc}") from exc
            return [path for path in result.stdout.split("\0") if path]

        paths = set(git("diff", "--name-only", "--diff-filter=ACMR", "-z",
                        baseline_shas.get(repository, "HEAD"), "--"))
        paths.update(git("ls-files", "--others", "--exclude-standard", "-z"))
        required: dict[str, list[str]] = {}
        uncovered = []
        for name in sorted(paths):
            path = Path(name)
            python_test = path.suffix == ".py" and (path.name.startswith("test_") or path.name.endswith("_test.py"))
            frontend_test = repository == "foyr2" and (name.lower().endswith("test.js") or name.endswith((".spec.ts", ".spec.tsx", ".node.tsx")))
            if not (python_test or frontend_test):
                continue
            if not (checkout / path).is_file():
                continue
            suite = None
            if repository == "cpm":
                if name.startswith("Test/unit/"):
                    suite = "python"
                elif name.startswith("Test/functional/"):
                    suite = "functional"
            elif repository == "foyr2":
                if python_test and name.startswith("tests/"):
                    suite = "python"
                elif frontend_test:
                    try:
                        validate_foyr_frontend_selector(name)
                    except ValueError:
                        pass
                    else:
                        suite = "frontend"
            elif repository == "pi":
                suite = "python"
            if suite is None or suite not in enabled_suites:
                uncovered.append(name)
            else:
                required.setdefault(suite, []).append(name)
        for suite, files in required.items():
            target = next((item for item in targets if item["repository"] == repository and item["suite"] == suite), None)
            if target is None:
                target = {"repository": repository, "suite": suite, "test_args": [], "reason": ""}
                targets.append(target)
            selectors, _ = split_pytest_arguments(target["test_args"])
            # Filters and stop-on-failure flags could exclude newly introduced cases.
            target["test_args"] = [selector for selector in selectors if selector.split("::", 1)[0] not in files] + files
            target["reason"] = (target["reason"] + " Run all cases in added or modified test files from the branch diff.").strip()
        if uncovered:
            reason = "Changed tests cannot run in the configured suites: " + ", ".join(uncovered)
            gap = next((item for item in skipped if item["repository"] == repository), None)
            if gap:
                gap["reason"] += " " + reason
            else:
                skipped.append({"repository": repository, "reason": reason})
    return DevelopmentVerificationRequest.model_validate({
        **request.model_dump(), "targets": targets, "skipped": skipped,
    })


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


class VerificationTargetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: Literal["cpm", "foyr2", "pi"]
    suite: Literal["python", "functional", "frontend", "karma", "smoke"]
    test_args: list[str]
    reason: str = ""
    status: Literal["passed", "failed", "environment_error"]
    returncode: StrictInt
    setup_returncode: StrictInt | None = None


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed", "environment_error", "partial", "not_run", "stale"]
    workspace_diff_hash: str | None
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    finished_at: datetime
    results: list[VerificationTargetResult] = Field(max_length=10)
    skipped: list[SkippedVerificationTarget] = Field(max_length=3)


def verification_request_hash(request: DevelopmentVerificationRequest) -> str:
    """Bind results to the actual selected commands as well as the code diff."""
    return hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()


def verification_status(results: list[dict], *, skipped: bool, unchanged: bool = True) -> str:
    codes = [result["returncode"] for result in results]
    return ("stale" if not unchanged else "environment_error" if 125 in codes
            else "failed" if any(codes) else "not_run" if not results
            else "partial" if skipped else "passed")


def validate_verification_report(
    value: object, request: DevelopmentVerificationRequest, *, command_succeeded: bool,
) -> dict:
    report = VerificationReport.model_validate(value)
    if (report.request_hash != verification_request_hash(request)
            or report.workspace_diff_hash != request.workspace_diff_hash):
        raise ValueError("host verification result is not bound to the selected tests and code")
    if len(report.results) != len(request.targets) or report.skipped != request.skipped:
        raise ValueError("host verification result does not cover every selected or skipped target")
    for result, target in zip(report.results, request.targets):
        if (result.repository, result.suite, result.test_args) != (target.repository, target.suite, target.test_args):
            raise ValueError("host verification result does not match the selected test commands")
        expected = "passed" if result.returncode == 0 else "environment_error" if result.returncode == 125 else "failed"
        if result.status != expected:
            raise ValueError("host verification target status contradicts its exit code")
        if result.setup_returncode is not None and result.setup_returncode != 0 and result.status != "environment_error":
            raise ValueError("host verification cannot pass after runtime setup failed")
    data = report.model_dump(mode="json", exclude_none=True)
    # Keep the nullable diff binding in legacy requests that have no code snapshot.
    data["workspace_diff_hash"] = report.workspace_diff_hash
    expected = verification_status(data["results"], skipped=bool(report.skipped), unchanged=report.status != "stale")
    if report.status != expected:
        raise ValueError("host verification summary contradicts its target results")
    if command_succeeded != (report.status in {"passed", "partial", "not_run"}):
        raise ValueError("host verification command exit contradicts its report")
    return data


def fixed_test_command(target: DevelopmentVerificationTarget) -> list[str]:
    if target.repository == "foyr2" and target.suite != "smoke":
        raise ValueError("Foyr verification must use the standalone stack")
    if target.suite == "smoke":
        return ["symphony-local-smoke", *target.test_args]
    if target.suite == "karma":
        return ["bash", "/symphony-runtime/foyr-frontend-tests.sh", target.test_args[0]]
    if target.repository == "cpm":
        raise ValueError("CPM verification must use the schema stack")
    return ["hatch", "run", "dev:pytest", *target.test_args]


def split_pytest_arguments(arguments: list[str]) -> tuple[list[str], list[str]]:
    """Separate validated selectors from pytest options for the schema runner."""
    selectors, options = [], []
    value_expected = False
    for argument in arguments:
        if value_expected or argument.startswith("-"):
            options.append(argument)
            value_expected = not value_expected and argument in {"-k", "-m", "--maxfail", "--tb", "-r"}
        else:
            selectors.append(argument)
    return selectors, options


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
    cpm_entrypoint: Path = CPM_VERIFICATION_ENTRYPOINT,
    foyr_entrypoint: Path = FOYR_VERIFICATION_ENTRYPOINT,
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
        elif target.repository == "cpm" and target.suite in {"python", "functional"}:
            action = "unit" if target.suite == "python" else target.suite
            selectors, options = split_pytest_arguments(target.test_args)
            command = [sys.executable, str(cpm_entrypoint), action,
                       "--source", str(workspace_path / "cpm"),
                       "--timeout", str(max(1, int(deadline - time.monotonic()) - 10))]
            command.extend(selectors)
            command.extend("--pytest-arg=" + option for option in options)
            if not cpm_entrypoint.is_file():
                print(f"CPM verification controller missing: {cpm_entrypoint}", flush=True)
                code = 125
            else:
                code = invoke(command)
        elif target.repository == "foyr2" and target.suite in {"python", "frontend", "karma"}:
            action = "python" if target.suite == "python" else "frontend"
            selectors, options = split_pytest_arguments(target.test_args) if action == "python" else (target.test_args, [])
            if target.suite == "karma":
                selectors = ["foyr/client_src/karma_test/" + item for item in selectors]
            command = [sys.executable, str(foyr_entrypoint), action,
                       "--source", str(workspace_path / "foyr2"),
                       "--timeout", str(max(1, int(deadline - time.monotonic()) - 10)),
                       *selectors, *("--pytest-arg=" + option for option in options)]
            if not foyr_entrypoint.is_file():
                print(f"Foyr verification controller missing: {foyr_entrypoint}", flush=True)
                code = 125
            else:
                code = invoke(command)
        elif target.suite == "functional":
            print("CPM schema verification entrypoint is not configured", flush=True)
            code = 125
        else:
            if runtime_entrypoint and not prepare_with_test and target.suite == "python" and target.repository not in prepared:
                prepared[target.repository] = invoke([
                    str(runtime_entrypoint), "up", str(workspace_path), target.repository,
                ])
            code = prepared.get(target.repository, 0) if target.suite == "python" else 0
            # Runtime startup failures mean no tests ran, regardless of the
            # setup script's exit code. Do not present these as test failures.
            if code != 0:
                code = 125
            if code == 0:
                command = [str(entrypoint), target.repository, str(workspace_path)]
                if runtime_entrypoint and not prepare_with_test:
                    command.append("--test-only")
                code = invoke([*command, "--", *fixed_test_command(target)])
        state = "passed" if code == 0 else "environment_error" if code == 125 else "failed"
        results.append({"repository": target.repository, "suite": target.suite,
                        "test_args": target.test_args, "reason": target.reason,
                        "status": state, "returncode": code})
        if target.suite == "python" and prepared.get(target.repository, 0) != 0:
            results[-1]["setup_returncode"] = prepared[target.repository]
        if code:
            failures.append(code)
    try:
        unchanged = unchanged and code_matches()
    except (HumanReviewContextError, ValueError):
        unchanged = False
    status = verification_status(results, skipped=bool(request.skipped), unchanged=unchanged)
    report = {"status": status, "workspace_diff_hash": request.workspace_diff_hash,
              "request_hash": verification_request_hash(request),
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
    parser.add_argument("--cpm-entrypoint", default=str(CPM_VERIFICATION_ENTRYPOINT),
                        help="Trusted schema-only CPM verification controller")
    parser.add_argument("--foyr-entrypoint", default=str(FOYR_VERIFICATION_ENTRYPOINT))
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
        cpm_entrypoint=Path(args.cpm_entrypoint).expanduser().resolve(),
        foyr_entrypoint=Path(args.foyr_entrypoint).expanduser().resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
