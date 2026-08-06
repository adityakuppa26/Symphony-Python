from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from .config import CodexConfig
from .environment import filtered_subprocess_environment
from .human_review import write_frozen_text_artifact


EventCallback = Callable[[int, str, dict[str, Any]], None | Awaitable[None]]
LogCallback = Callable[[str, str], None | Awaitable[None]]


@dataclass
class CodexRunResult:
    status: str
    returncode: int | None
    final_message: str | None
    error: str | None
    stderr_path: Path
    final_message_path: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    raw_stdout_lines: list[str] = field(default_factory=list)


class CodexRunner:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        excluded_environment_names: Iterable[str | None] = (),
    ) -> None:
        self._environ = environ
        self._excluded_environment_names = frozenset(
            name for name in excluded_environment_names if name
        )

    async def run(
        self,
        prompt: str,
        workspace_path: Path,
        config: CodexConfig,
        *,
        timeout_seconds: int,
        event_callback: EventCallback | None = None,
        log_callback: LogCallback | None = None,
    ) -> CodexRunResult:
        symphony_dir = workspace_path / ".symphony"
        stderr_path = symphony_dir / "codex-stderr.log"
        final_message_path = workspace_path / config.output_last_message_file
        prompt_bytes = prompt.encode("utf-8")

        process_options: dict[str, Any] = {}
        if os.name == "posix":
            process_options["start_new_session"] = True

        process = await asyncio.create_subprocess_exec(
            config.command,
            *config.args,
            cwd=str(workspace_path),
            env=self._subprocess_environment(config),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_options,
        )

        events: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        final_message: str | None = None

        async def read_stdout() -> None:
            nonlocal final_message
            sequence = 0
            assert process.stdout is not None
            buffer = b""
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    sequence = await process_stdout_line(line_bytes, sequence)
            if buffer:
                await process_stdout_line(buffer, sequence)

        async def process_stdout_line(line_bytes: bytes, sequence: int) -> int:
            nonlocal final_message
            line = line_bytes.decode(errors="replace").rstrip("\r")
            if not line:
                return sequence
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                raw_lines.append(line)
                if log_callback:
                    await maybe_await(log_callback("codex_stdout", line))
                return sequence
            sequence += 1
            events.append(raw)
            event_type = event_type_from(raw)
            message = final_message_from_event(raw, event_type)
            if message:
                final_message = message
            if event_callback:
                await maybe_await(event_callback(sequence, event_type, raw))
            return sequence

        async def read_stderr() -> str:
            assert process.stderr is not None
            data = await process.stderr.read()
            text = data.decode(errors="replace")
            write_frozen_text_artifact(
                workspace_path,
                ".symphony/codex-stderr.log",
                text,
                label="Codex stderr artifact",
            )
            return text

        async def write_stdin() -> bool:
            assert process.stdin is not None
            delivered = False
            cancelled = False
            try:
                for offset in range(0, len(prompt_bytes), 65536):
                    process.stdin.write(prompt_bytes[offset : offset + 65536])
                    await process.stdin.drain()
                delivered = True
            except asyncio.CancelledError:
                cancelled = True
                raise
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    process.stdin.close()
                    if not cancelled:
                        await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    delivered = False
            return delivered

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        stdin_task = asyncio.create_task(write_stdin())

        async def terminate_and_settle() -> int | None:
            if not stdin_task.done():
                stdin_task.cancel()
            returncode = await terminate_runner_process(
                process,
                terminate_timeout_seconds=5,
            )
            for task in (stdin_task, stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                stdin_task,
                stdout_task,
                stderr_task,
                return_exceptions=True,
            )
            if not stderr_path.exists():
                write_frozen_text_artifact(
                    workspace_path,
                    ".symphony/codex-stderr.log",
                    "",
                    label="Codex stderr artifact",
                )
            return returncode

        async def wait_for_completion() -> tuple[int, bool, str]:
            returncode = await process.wait()
            prompt_delivered = await stdin_task
            await stdout_task
            stderr_text = await stderr_task
            return returncode, prompt_delivered, stderr_text

        timed_out = False
        try:
            try:
                returncode, prompt_delivered, stderr_text = await asyncio.wait_for(
                    wait_for_completion(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                timed_out = True
                returncode = await terminate_and_settle()
                prompt_delivered = False
                stderr_text = ""
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(terminate_and_settle())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup_task)
            raise
        except Exception:
            await terminate_and_settle()
            raise

        if final_message is None:
            final_message = final_message_from_events(events)
        write_frozen_text_artifact(
            workspace_path,
            config.output_last_message_file,
            final_message or "",
            label="Codex final-message artifact",
        )

        if timed_out:
            return CodexRunResult(
                status="failed",
                returncode=returncode,
                final_message=final_message,
                error=f"Codex timed out after {timeout_seconds} seconds",
                stderr_path=stderr_path,
                final_message_path=final_message_path,
                events=events,
                raw_stdout_lines=raw_lines,
            )

        if not prompt_delivered and returncode == 0:
            return CodexRunResult(
                status="failed",
                returncode=returncode,
                final_message=final_message,
                error="Codex exited before the complete prompt was delivered over stdin",
                stderr_path=stderr_path,
                final_message_path=final_message_path,
                events=events,
                raw_stdout_lines=raw_lines,
            )

        if returncode == 0:
            return CodexRunResult(
                status="completed",
                returncode=returncode,
                final_message=final_message,
                error=None,
                stderr_path=stderr_path,
                final_message_path=final_message_path,
                events=events,
                raw_stdout_lines=raw_lines,
            )

        combined = "\n".join(raw_lines + [stderr_text]).strip()
        blocked = looks_blocked(combined)
        return CodexRunResult(
            status="blocked" if blocked else "failed",
            returncode=returncode,
            final_message=final_message,
            error=combined or f"Codex exited with status {returncode}",
            stderr_path=stderr_path,
            final_message_path=final_message_path,
            events=events,
            raw_stdout_lines=raw_lines,
        )

    def _subprocess_environment(self, config: CodexConfig) -> dict[str, str]:
        source = os.environ if self._environ is None else self._environ
        return filtered_subprocess_environment(
            source,
            excluded_names=self._excluded_environment_names,
            excluded_patterns=config.environment_exclude,
        )


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def terminate_process(process: Any, *, terminate_timeout_seconds: int) -> int | None:
    try:
        process.terminate()
    except ProcessLookupError:
        return await process.wait()
    try:
        return await asyncio.wait_for(process.wait(), timeout=terminate_timeout_seconds)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return await process.wait()
        return await process.wait()


async def terminate_runner_process(
    process: Any,
    *,
    terminate_timeout_seconds: int,
) -> int | None:
    """Terminate the isolated Codex process group, with a portable fallback."""
    process_id = getattr(process, "pid", None)
    if os.name != "posix" or not isinstance(process_id, int):
        return await terminate_process(
            process,
            terminate_timeout_seconds=terminate_timeout_seconds,
        )

    wait_task = asyncio.create_task(process.wait())
    _signal_process_group(process_id, signal.SIGTERM)

    async def wait_until_group_exits() -> int | None:
        returncode = await asyncio.shield(wait_task)
        while _process_group_exists(process_id):
            await asyncio.sleep(0.05)
        return returncode

    try:
        return await asyncio.wait_for(
            wait_until_group_exits(),
            timeout=terminate_timeout_seconds,
        )
    except asyncio.TimeoutError:
        _signal_process_group(process_id, signal.SIGKILL)
        try:
            return await asyncio.wait_for(
                asyncio.shield(wait_task),
                timeout=terminate_timeout_seconds,
            )
        except asyncio.TimeoutError:
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)
            return getattr(process, "returncode", None)


def _signal_process_group(process_group_id: int, signal_number: signal.Signals) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        pass


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def event_type_from(raw: dict[str, Any]) -> str:
    for key in ("type", "event_type"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    for parent_key in ("msg", "event"):
        parent = raw.get(parent_key)
        if isinstance(parent, dict):
            value = parent.get("type") or parent.get("event_type")
            if isinstance(value, str):
                return value
    return "unknown"


def final_message_from_events(events: list[dict[str, Any]]) -> str | None:
    message: str | None = None
    for raw in events:
        candidate = final_message_from_event(raw, event_type_from(raw))
        if candidate:
            message = candidate
    return message


def final_message_from_event(raw: dict[str, Any], event_type: str) -> str | None:
    item = raw.get("item")
    if item is None and isinstance(raw.get("msg"), dict):
        item = raw["msg"].get("item")
    if item is None and isinstance(raw.get("event"), dict):
        item = raw["event"].get("item")

    if event_type == "item.completed" and isinstance(item, dict):
        role = item.get("role")
        item_type = item.get("type")
        if role == "assistant" or item_type in {"message", "agent_message", "assistant_message"}:
            return content_to_text(item.get("content") or item.get("text") or item.get("message"))

    if event_type == "turn.completed":
        for key in ("final_message", "message", "summary", "output"):
            value = raw.get(key)
            text = content_to_text(value)
            if text:
                return text
        turn = raw.get("turn")
        if isinstance(turn, dict):
            for key in ("final_message", "message", "summary", "output"):
                text = content_to_text(turn.get(key))
                if text:
                    return text
    return None


def content_to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [content_to_text(item) for item in value]
        text = "".join(part for part in parts if part)
        return text or None
    if isinstance(value, dict):
        for key in ("text", "content", "message", "value"):
            if key in value:
                text = content_to_text(value[key])
                if text:
                    return text
    return str(value)


def looks_blocked(text: str) -> bool:
    lowered = text.lower()
    needles = [
        "approval",
        "approval denied",
        "requires approval",
        "requires confirmation",
        "confirmation required",
        "user input",
        "input required",
        "waiting for user",
        "sandbox",
        "sandbox denied",
        "permission denied",
        "operation not permitted",
        "read-only file system",
        "not allowed",
        "mcp elicitation",
        "tool requires approval",
        "human intervention",
        "blocked",
    ]
    return any(needle in lowered for needle in needles)
