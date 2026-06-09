from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import CodexConfig


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
        symphony_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = symphony_dir / "codex-stderr.log"
        final_message_path = workspace_path / config.output_last_message_file
        final_message_path.parent.mkdir(parents=True, exist_ok=True)

        process = await asyncio.create_subprocess_exec(
            config.command,
            *config.args,
            prompt,
            cwd=str(workspace_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
            stderr_path.write_text(text, encoding="utf-8")
            return text

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())

        timed_out = False
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            process.terminate()
            try:
                returncode = await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                returncode = await process.wait()

        await stdout_task
        stderr_text = await stderr_task

        if final_message is None:
            final_message = final_message_from_events(events)
        if final_message:
            final_message_path.write_text(final_message, encoding="utf-8")
        else:
            final_message_path.write_text("", encoding="utf-8")

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


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


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
