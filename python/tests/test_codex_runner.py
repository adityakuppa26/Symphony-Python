from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path

from symphony_jira.codex_runner import (
    CodexRunner,
    looks_blocked,
    terminate_process,
    terminate_runner_process,
)
from symphony_jira.config import CodexConfig


class CodexRunnerTests(unittest.TestCase):
    def test_parses_jsonl_events_and_extracts_final_agent_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()
            events: list[tuple[int, str, dict]] = []

            async def run() -> None:
                result = await CodexRunner().run(
                    "prompt",
                    workspace,
                    CodexConfig(command=str(fake_codex), args=["exec", "--json"]),
                    timeout_seconds=30,
                    event_callback=lambda seq, event_type, raw: events.append((seq, event_type, raw)),
                )
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.final_message, "final report")
                self.assertEqual(result.raw_stdout_lines, ["not json"])

            asyncio.run(run())

            self.assertEqual(events[0][1], "item.completed")
            self.assertEqual((workspace / ".symphony" / "codex-final.md").read_text(encoding="utf-8"), "final report")
            self.assertEqual((workspace / ".symphony" / "codex-stderr.log").read_text(encoding="utf-8"), "stderr line\n")

    def test_blocked_classifier_detects_sandbox_and_approval_failures(self) -> None:
        self.assertTrue(looks_blocked("tool requires approval before continuing"))
        self.assertTrue(looks_blocked("sandbox denied write access"))
        self.assertTrue(looks_blocked("waiting for user input"))
        self.assertFalse(looks_blocked("unit test failed with assertion error"))

    def test_parses_large_jsonl_event_without_readline_limit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_large_event_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()

            async def run() -> None:
                result = await CodexRunner().run(
                    "prompt",
                    workspace,
                    CodexConfig(command=str(fake_codex), args=["exec", "--json"]),
                    timeout_seconds=30,
                )
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.final_message, "final report")
                self.assertEqual(len(result.events), 2)

            asyncio.run(run())

    def test_streams_large_utf8_prompt_over_stdin_with_bounded_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_stdin_inspection_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()
            prompt = ("Jira requirement λ🙂 with exact bytes\n" * 100_000) + "final marker"
            expected_bytes = prompt.encode("utf-8")

            async def run() -> None:
                result = await CodexRunner().run(
                    prompt,
                    workspace,
                    CodexConfig(
                        command=str(fake_codex),
                        args=[
                            "exec",
                            "--json",
                            "--sandbox",
                            "workspace-write",
                            "-c",
                            'model_reasoning_effort="low"',
                        ],
                    ),
                    timeout_seconds=30,
                )
                self.assertEqual(result.status, "completed")
                observed = json.loads(result.final_message or "{}")
                self.assertEqual(
                    observed["argv"],
                    [
                        "exec",
                        "--json",
                        "--sandbox",
                        "workspace-write",
                        "-c",
                        'model_reasoning_effort="low"',
                    ],
                )
                self.assertEqual(observed["byte_length"], len(expected_bytes))
                self.assertEqual(observed["sha256"], hashlib.sha256(expected_bytes).hexdigest())

            asyncio.run(run())

    def test_nonzero_child_error_wins_over_incomplete_prompt_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_closed_stdin_fake_codex(
                root,
                returncode=9,
                stderr="real Codex failure",
            )
            workspace = root / "workspace"
            workspace.mkdir()

            async def run() -> None:
                result = await CodexRunner().run(
                    "private Jira requirement " * 500_000,
                    workspace,
                    CodexConfig(command=str(fake_codex), args=["exec", "--json"]),
                    timeout_seconds=30,
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.returncode, 9)
                self.assertEqual(result.error, "real Codex failure")

            asyncio.run(run())

    def test_timeout_covers_io_after_parent_exits_with_inherited_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_inherited_pipe_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()

            async def run() -> None:
                started = asyncio.get_running_loop().time()
                try:
                    result = await asyncio.wait_for(
                        CodexRunner().run(
                            "x" * 4_000_000,
                            workspace,
                            CodexConfig(command=str(fake_codex), args=["exec", "--json"]),
                            timeout_seconds=0.05,
                        ),
                        timeout=2,
                    )
                    elapsed = asyncio.get_running_loop().time() - started
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.error, "Codex timed out after 0.05 seconds")
                    self.assertLess(elapsed, 1)
                    grandchild_pid = int(
                        (workspace / "grandchild.pid").read_text(encoding="utf-8")
                    )
                    self.assertFalse(process_is_running(grandchild_pid))
                finally:
                    pid_path = workspace / "grandchild.pid"
                    if pid_path.exists():
                        grandchild_pid = int(pid_path.read_text(encoding="utf-8"))
                        try:
                            if process_is_running(grandchild_pid):
                                os.kill(grandchild_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    await asyncio.sleep(0.05)

            asyncio.run(run())

    def test_fails_if_child_exits_before_complete_prompt_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_closed_stdin_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()
            prompt = "private Jira requirement " * 500_000

            async def run() -> None:
                result = await CodexRunner().run(
                    prompt,
                    workspace,
                    CodexConfig(command=str(fake_codex), args=["exec", "--json"]),
                    timeout_seconds=30,
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(
                    result.error,
                    "Codex exited before the complete prompt was delivered over stdin",
                )
                self.assertNotIn("private Jira requirement", result.error or "")

            asyncio.run(run())

    def test_timeout_settles_blocked_stdin_writer_and_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_hanging_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()

            async def run() -> None:
                result = await asyncio.wait_for(
                    CodexRunner().run(
                        "x" * 4_000_000,
                        workspace,
                        CodexConfig(command=str(fake_codex), args=["exec", "--json"]),
                        timeout_seconds=0.05,
                    ),
                    timeout=10,
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error, "Codex timed out after 0.05 seconds")
                assert_child_process_gone(workspace / "child.pid")

            asyncio.run(run())

    def test_cancellation_settles_blocked_stdin_writer_and_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_hanging_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()

            async def run() -> None:
                task = asyncio.create_task(
                    CodexRunner().run(
                        "x" * 4_000_000,
                        workspace,
                        CodexConfig(command=str(fake_codex), args=["exec", "--json"]),
                        timeout_seconds=30,
                    )
                )
                pid_path = workspace / "child.pid"
                for _ in range(100):
                    if pid_path.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(pid_path.exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=10)
                assert_child_process_gone(pid_path)

            asyncio.run(run())

    def test_timeout_cleanup_ignores_process_that_already_exited(self) -> None:
        async def run() -> None:
            process = AlreadyExitedProcess()
            returncode = await terminate_process(process, terminate_timeout_seconds=1)

            self.assertEqual(returncode, 0)
            self.assertTrue(process.terminate_called)

        asyncio.run(run())

    def test_timeout_cleanup_escalates_from_terminate_to_kill(self) -> None:
        async def run() -> None:
            process = TerminateThenKillProcess()
            returncode = await terminate_process(process, terminate_timeout_seconds=0.01)

            self.assertEqual(returncode, -9)
            self.assertTrue(process.terminate_called)
            self.assertTrue(process.kill_called)

        asyncio.run(run())

    @unittest.skipUnless(os.name == "posix", "POSIX process groups are required")
    def test_group_cleanup_is_bounded_if_descendant_escapes_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_escaped_pipe_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()

            async def run() -> None:
                process = await asyncio.create_subprocess_exec(
                    str(fake_codex),
                    cwd=str(workspace),
                    start_new_session=True,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                pid_path = workspace / "escaped.pid"
                escaped_pid: int | None = None
                for _ in range(100):
                    try:
                        escaped_pid = int(pid_path.read_text(encoding="utf-8"))
                    except (FileNotFoundError, ValueError):
                        await asyncio.sleep(0.01)
                        continue
                    break
                self.assertTrue(pid_path.exists())
                self.assertIsNotNone(escaped_pid)
                assert escaped_pid is not None
                started = asyncio.get_running_loop().time()
                try:
                    returncode = await asyncio.wait_for(
                        terminate_runner_process(
                            process,
                            terminate_timeout_seconds=0.02,
                        ),
                        timeout=1,
                    )
                    elapsed = asyncio.get_running_loop().time() - started
                    self.assertEqual(returncode, process.returncode)
                    self.assertLess(elapsed, 0.5)
                    self.assertTrue(process_is_running(escaped_pid))
                finally:
                    try:
                        os.kill(escaped_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if process.stdin is not None:
                        process.stdin.close()
                    await asyncio.wait_for(process.wait(), timeout=1)
                    if process.stdout is not None and process.stderr is not None:
                        await asyncio.gather(process.stdout.read(), process.stderr.read())
                    if process.stdin is not None:
                        try:
                            await process.stdin.wait_closed()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    await asyncio.sleep(0)

            asyncio.run(run())

    def test_child_environment_excludes_globs_and_exact_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_environment_fake_codex(root)
            workspace = root / "workspace"
            workspace.mkdir()
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "JIRA_TOKEN": "jira-secret",
                "PROJECT_JIRA_TOKEN": "project-jira-secret",
                "WORKFLOW_SECRET_ONE": "workflow-secret",
                "CUSTOM_TRACKER_CREDENTIAL": "custom-secret",
                "VISIBLE_TO_CODEX": "visible",
            }
            runner = CodexRunner(
                environ=environment,
                excluded_environment_names={"CUSTOM_TRACKER_CREDENTIAL"},
            )
            config = CodexConfig(
                command=str(fake_codex),
                args=["exec", "--json"],
                environment_exclude=["JIRA_*", "*_JIRA_TOKEN", "WORKFLOW_SECRET_*"],
            )

            async def run() -> None:
                result = await runner.run(
                    "prompt",
                    workspace,
                    config,
                    timeout_seconds=30,
                )
                self.assertEqual(result.status, "completed")
                child_environment = json.loads(result.final_message or "{}")
                self.assertEqual(child_environment["VISIBLE_TO_CODEX"], "visible")
                self.assertIsNone(child_environment["JIRA_TOKEN"])
                self.assertIsNone(child_environment["PROJECT_JIRA_TOKEN"])
                self.assertIsNone(child_environment["WORKFLOW_SECRET_ONE"])
                self.assertIsNone(child_environment["CUSTOM_TRACKER_CREDENTIAL"])

            asyncio.run(run())


def write_fake_codex(root: Path) -> Path:
    path = root / "fake_codex.py"
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
if "--version" in sys.argv:
    print("fake codex 0.0")
    sys.exit(0)
print(json.dumps({"type": "item.completed", "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "final report"}]}}))
print("not json")
print("stderr line", file=sys.stderr)
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def write_environment_fake_codex(root: Path) -> Path:
    path = root / "fake_environment_codex.py"
    path.write_text(
        """#!/usr/bin/python3
import json
import os

names = [
    "JIRA_TOKEN",
    "PROJECT_JIRA_TOKEN",
    "WORKFLOW_SECRET_ONE",
    "CUSTOM_TRACKER_CREDENTIAL",
    "VISIBLE_TO_CODEX",
]
environment = {name: os.environ.get(name) for name in names}
print(json.dumps({
    "type": "item.completed",
    "item": {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": json.dumps(environment)}],
    },
}))
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def write_stdin_inspection_fake_codex(root: Path) -> Path:
    path = root / "fake_stdin_codex.py"
    path.write_text(
        """#!/usr/bin/python3
import hashlib
import json
import sys

prompt = sys.stdin.buffer.read()
observed = {
    "argv": sys.argv[1:],
    "byte_length": len(prompt),
    "sha256": hashlib.sha256(prompt).hexdigest(),
}
print(json.dumps({
    "type": "item.completed",
    "item": {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": json.dumps(observed)}],
    },
}))
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def write_closed_stdin_fake_codex(
    root: Path,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> Path:
    path = root / "fake_closed_stdin_codex.py"
    path.write_text(
        f"""#!/usr/bin/python3
import os
import sys
import time

os.close(0)
time.sleep(0.05)
print({stderr!r}, file=sys.stderr, end="")
raise SystemExit({returncode})
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def write_inherited_pipe_fake_codex(root: Path) -> Path:
    path = root / "fake_inherited_pipe_codex.py"
    path.write_text(
        """#!/usr/bin/python3
import os
from pathlib import Path
import time

grandchild = os.fork()
if grandchild == 0:
    Path("grandchild.ready").write_text("ready", encoding="utf-8")
    time.sleep(60)
    os._exit(0)
while not Path("grandchild.ready").exists():
    time.sleep(0.001)
Path("grandchild.pid").write_text(str(grandchild), encoding="utf-8")
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def write_hanging_fake_codex(root: Path) -> Path:
    path = root / "fake_hanging_codex.py"
    path.write_text(
        """#!/usr/bin/python3
import os
from pathlib import Path
import time

Path("child.pid").write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def write_escaped_pipe_fake_codex(root: Path) -> Path:
    path = root / "fake_escaped_pipe_codex.py"
    path.write_text(
        """#!/usr/bin/python3
import os
from pathlib import Path
import time

escaped = os.fork()
if escaped == 0:
    os.setsid()
    Path("escaped.pid").write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(60)
    os._exit(0)
while not Path("escaped.pid").exists():
    time.sleep(0.001)
time.sleep(60)
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


def assert_child_process_gone(pid_path: Path) -> None:
    pid = int(pid_path.read_text(encoding="utf-8"))
    with unittest.TestCase().assertRaises(ProcessLookupError):
        os.kill(pid, 0)


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        state = stat_path.read_text(encoding="utf-8").split()[2]
    except (FileNotFoundError, IndexError):
        return False
    return state != "Z"


class AlreadyExitedProcess:
    def __init__(self) -> None:
        self.terminate_called = False

    def terminate(self) -> None:
        self.terminate_called = True
        raise ProcessLookupError()

    async def wait(self) -> int:
        return 0


class TerminateThenKillProcess:
    def __init__(self) -> None:
        self.terminate_called = False
        self.kill_called = False

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True

    async def wait(self) -> int:
        if self.kill_called:
            return -9
        await asyncio.Future()
        raise AssertionError("unreachable")


def write_large_event_fake_codex(root: Path) -> Path:
    path = root / "fake_large_codex.py"
    path.write_text(
        """#!/usr/bin/env python3
import json
print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "x" * 200000}}))
print(json.dumps({"type": "item.completed", "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "final report"}]}}))
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


if __name__ == "__main__":
    unittest.main()
