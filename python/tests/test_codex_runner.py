from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from symphony_jira.codex_runner import CodexRunner, terminate_process, looks_blocked
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

    def test_timeout_cleanup_ignores_process_that_already_exited(self) -> None:
        async def run() -> None:
            process = AlreadyExitedProcess()
            returncode = await terminate_process(process, terminate_timeout_seconds=1)

            self.assertEqual(returncode, 0)
            self.assertTrue(process.terminate_called)

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


class AlreadyExitedProcess:
    def __init__(self) -> None:
        self.terminate_called = False

    def terminate(self) -> None:
        self.terminate_called = True
        raise ProcessLookupError()

    async def wait(self) -> int:
        return 0


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
