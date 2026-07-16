from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from symphony_jira.attachment_analysis import CodexAttachmentAnalyzer
from symphony_jira.config import JiraRequirementsConfig
from symphony_jira.models import (
    AttachmentAnalysis,
    IssueAttachment,
    RequirementSource,
)


class CodexAttachmentAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_analysis_uses_safe_flags_and_versioned_content_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "count.txt"
            args_file = root / "args.json"
            codex = write_fake_codex(root, counter=counter, args_file=args_file)
            cache_dir = root / ".symphony" / "attachment-analysis-cache"
            attachment = make_attachment("screen.png", "image/png")
            content = b"fake-png-evidence"

            first = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
                max_output_characters=1_000,
            )
            result = await first.analyze(attachment, content)

            self.assertEqual(result.status, "complete")
            self.assertEqual(result.modality, "vision")
            self.assertIn("Visible text/OCR", result.summary)
            arguments = json.loads(args_file.read_text(encoding="utf-8"))
            for flag in (
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--image",
            ):
                self.assertIn(flag, arguments)
            self.assertIn("untrusted content", arguments[-1])
            self.assertIn("do not enumerate absent roles", arguments[-1])
            for marker in ("[classification: current]", "[inferred]", "[contradiction]"):
                self.assertIn(marker, arguments[-1])
            self.assertIn("within the attached image(s)", arguments[-1])
            self.assertIn("sources you have not been shown", arguments[-1])

            self.assertEqual(counter.read_text(encoding="utf-8"), "1")
            digest = hashlib.sha256(content).hexdigest()
            cache_file = cache_dir / f"{digest}.json"
            self.assertTrue(cache_file.exists())

            # A new client instance reuses the durable result for unchanged bytes
            # and the same analyzer contract.
            second = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
                max_output_characters=1_000,
            )
            cached = await second.analyze(attachment, content)
            self.assertEqual(cached.summary, result.summary)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

            # Contract-affecting configuration intentionally refreshes the entry.
            revised = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
                max_output_characters=2_000,
            )
            await revised.analyze(attachment, content)
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

    async def test_engine_identity_change_invalidates_cached_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "count.txt"
            codex = write_fake_codex(
                root,
                counter=counter,
                version="fake-codex 1.0",
            )
            cache_dir = root / "cache"
            attachment = make_attachment("screen.png", "image/png")
            content = b"stable-image"

            first = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            )
            await first.analyze(attachment, content)
            self.assertEqual(first.engine_identity["version"]["output"], "fake-codex 1.0")
            original_contract_hash = first.contract_hash

            write_fake_codex(
                root,
                counter=counter,
                version="fake-codex 200.0",
                path=codex,
            )
            await first.analyze(attachment, content)
            self.assertNotEqual(first.contract_hash, original_contract_hash)
            self.assertEqual(first.engine_identity["version"]["output"], "fake-codex 200.0")
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")

            second = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            )
            self.assertEqual(second.contract_hash, first.contract_hash)
            await second.analyze(attachment, content)
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            digest = hashlib.sha256(content).hexdigest()
            payload = json.loads(
                (cache_dir / f"{digest}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["engine_identity"], first.engine_identity)

    async def test_cache_rejects_permissive_file_and_directory_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "count.txt"
            codex = write_fake_codex(root, counter=counter)
            cache_dir = root / "cache"
            attachment = make_attachment("screen.png", "image/png")
            content = b"private-cache-image"
            digest = hashlib.sha256(content).hexdigest()
            cache_file = cache_dir / f"{digest}.json"

            first = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            )
            await first.analyze(attachment, content)
            self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(cache_file.stat().st_mode), 0o600)

            cache_file.chmod(0o640)
            await CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            ).analyze(attachment, content)
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            self.assertEqual(stat.S_IMODE(cache_file.stat().st_mode), 0o600)

            cache_dir.chmod(0o750)
            await CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            ).analyze(attachment, content)
            self.assertEqual(counter.read_text(encoding="utf-8"), "3")
            self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(cache_file.stat().st_mode), 0o600)

    async def test_cache_entry_symlink_is_replaced_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "count.txt"
            codex = write_fake_codex(root, counter=counter)
            cache_dir = root / "cache"
            attachment = make_attachment("screen.png", "image/png")
            content = b"symlink-cache-image"
            digest = hashlib.sha256(content).hexdigest()
            cache_file = cache_dir / f"{digest}.json"

            analyzer = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            )
            await analyzer.analyze(attachment, content)
            target = root / "do-not-modify.json"
            cache_file.replace(target)
            target_bytes = target.read_bytes()
            cache_file.symlink_to(target)

            await CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            ).analyze(attachment, content)

            self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            self.assertFalse(cache_file.is_symlink())
            self.assertEqual(target.read_bytes(), target_bytes)
            self.assertEqual(stat.S_IMODE(cache_file.stat().st_mode), 0o600)

    async def test_cache_directory_symlink_is_never_followed_or_chmodded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "count.txt"
            codex = write_fake_codex(root, counter=counter)
            target_dir = root / "shared-target"
            target_dir.mkdir(mode=0o755)
            sentinel = target_dir / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            cache_dir = root / "cache"
            cache_dir.symlink_to(target_dir, target_is_directory=True)
            content = b"directory-symlink-image"

            result = await CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            ).analyze(make_attachment("screen.png", "image/png"), content)

            digest = hashlib.sha256(content).hexdigest()
            self.assertEqual(result.status, "complete")
            self.assertTrue(cache_dir.is_symlink())
            self.assertEqual(stat.S_IMODE(target_dir.stat().st_mode), 0o755)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse((target_dir / f"{digest}.json").exists())

    async def test_cache_rejects_wrong_owner_and_non_regular_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = write_fake_codex(root)
            cache_dir = root / "cache"
            content = b"owner-and-type-image"
            digest = hashlib.sha256(content).hexdigest()
            cache_file = cache_dir / f"{digest}.json"
            analyzer = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=cache_dir,
            )
            await analyzer.analyze(
                make_attachment("screen.png", "image/png"),
                content,
            )

            effective_uid = getattr(os, "geteuid", None)
            if effective_uid is not None:
                with mock.patch(
                    "symphony_jira.attachment_analysis.os.geteuid",
                    return_value=effective_uid() + 1,
                ):
                    self.assertIsNone(analyzer._read_cache(digest))

            cache_file.unlink()
            cache_file.mkdir(mode=0o700)
            self.assertIsNone(analyzer._read_cache(digest))

    async def test_text_delegates_to_basic_and_unsupported_gif_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            analyzer = CodexAttachmentAnalyzer(
                codex_command="missing-codex",
                cache_dir=Path(tmp) / "cache",
            )

            text_result = await analyzer.analyze(
                make_attachment("requirements.txt", "text/plain"),
                b"GC can see the Cost Code column.",
            )
            gif_result = await analyzer.analyze(
                make_attachment("animated.gif", "image/gif"),
                b"GIF89a",
            )

        self.assertEqual(text_result.status, "complete")
        self.assertEqual(text_result.modality, "text")
        self.assertEqual(gif_result.status, "unsupported")
        self.assertEqual(gif_result.modality, "vision")

    async def test_pdf_renders_bounded_pages_and_rejects_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "count.txt"
            args_file = root / "codex-args.json"
            render_args = root / "render-args.json"
            codex = write_fake_codex(root, counter=counter, args_file=args_file)
            renderer = write_fake_pdftoppm(root, page_count=2, args_file=render_args)
            analyzer = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                pdftoppm_command=str(renderer),
                cache_dir=root / "cache",
                pdf_max_pages=2,
            )

            complete = await analyzer.analyze(
                make_attachment("mockups.pdf", "application/pdf"),
                b"two-page-pdf",
            )

            self.assertEqual(complete.status, "complete")
            self.assertEqual(complete.modality, "ocr")
            renderer_arguments = json.loads(render_args.read_text(encoding="utf-8"))
            self.assertEqual(renderer_arguments[renderer_arguments.index("-l") + 1], "3")
            self.assertEqual(json.loads(args_file.read_text(encoding="utf-8")).count("--image"), 2)

            overbound_renderer = write_fake_pdftoppm(
                root,
                page_count=3,
                args_file=root / "overbound-render-args.json",
                name="fake-pdftoppm-overbound.py",
            )
            overbound = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                pdftoppm_command=str(overbound_renderer),
                cache_dir=root / "other-cache",
                pdf_max_pages=2,
            )
            incomplete = await overbound.analyze(
                make_attachment("larger.pdf", "application/pdf"),
                b"three-page-pdf",
            )

            self.assertEqual(incomplete.status, "skipped")
            self.assertIn("more than the configured 2 pages", incomplete.summary)
            self.assertEqual(counter.read_text(encoding="utf-8"), "1")

    async def test_missing_pdf_renderer_and_timeout_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = write_fake_codex(root, sleep_seconds=1.0)
            missing_renderer = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                pdftoppm_command=str(root / "missing-pdftoppm"),
                cache_dir=root / "cache",
            )
            pdf_result = await missing_renderer.analyze(
                make_attachment("evidence.pdf", "application/pdf"),
                b"pdf",
            )
            timeout = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=root / "other-cache",
                timeout_seconds=0.05,
            )
            timeout_result = await timeout.analyze(
                make_attachment("screen.png", "image/png"),
                b"image",
            )

        self.assertEqual(pdf_result.status, "not_configured")
        self.assertIn("pdftoppm", pdf_result.summary)
        self.assertEqual(timeout_result.status, "error")
        self.assertIn("timed out", timeout_result.summary)

    async def test_global_semaphore_bounds_different_attachment_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "concurrency.txt"
            codex = write_concurrency_codex(root, state_file)
            analyzer = CodexAttachmentAnalyzer(
                codex_command=str(codex),
                cache_dir=root / "cache",
                max_concurrency=1,
            )
            attachment = make_attachment("screen.png", "image/png")

            results = await asyncio.gather(
                analyzer.analyze(attachment, b"first-image"),
                analyzer.analyze(attachment, b"second-image"),
            )

            current, maximum, total = [
                int(value) for value in state_file.read_text(encoding="utf-8").split(",")
            ]
        self.assertEqual([item.status for item in results], ["complete", "complete"])
        self.assertEqual(current, 0)
        self.assertEqual(maximum, 1)
        self.assertEqual(total, 2)


class AttachmentAnalysisConfigTests(unittest.TestCase):
    def test_defaults_and_hard_upper_bounds(self) -> None:
        config = JiraRequirementsConfig()
        self.assertEqual(config.attachment_analyzer, "basic")
        self.assertEqual(config.attachment_analysis_max_concurrency, 1)
        with self.assertRaises(ValidationError):
            JiraRequirementsConfig(attachment_analysis_timeout_seconds=901)
        with self.assertRaises(ValidationError):
            JiraRequirementsConfig(attachment_pdf_max_pages=21)
        with self.assertRaises(ValidationError):
            JiraRequirementsConfig(attachment_analysis_max_concurrency=5)


def make_attachment(filename: str, mime_type: str) -> IssueAttachment:
    source = RequirementSource(
        issue_identifier="ICPM-1",
        source_type="attachment",
        source_id=f"attachment:{filename}",
    )
    return IssueAttachment(
        id=filename,
        filename=filename,
        mime_type=mime_type,
        source=source,
        analysis=AttachmentAnalysis(
            status="not_configured",
            modality="unknown",
            summary="pending",
        ),
    )


def write_fake_codex(
    root: Path,
    *,
    counter: Path | None = None,
    args_file: Path | None = None,
    sleep_seconds: float = 0,
    version: str = "fake-codex 1.0",
    path: Path | None = None,
) -> Path:
    path = path or root / f"fake-codex-{len(list(root.glob('fake-codex-*.py')))}.py"
    script = f"""#!/usr/bin/env python3
import json
import pathlib
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print({version!r})
    raise SystemExit(0)
time.sleep({sleep_seconds!r})
counter = pathlib.Path({str(counter)!r}) if {counter is not None!r} else None
if counter is not None:
    count = int(counter.read_text() or "0") if counter.exists() else 0
    counter.write_text(str(count + 1))
args_file = pathlib.Path({str(args_file)!r}) if {args_file is not None!r} else None
if args_file is not None:
    args_file.write_text(json.dumps(args))
output = pathlib.Path(args[args.index("--output-last-message") + 1])
output.write_text("Visible text/OCR: Cost Code\\nRoles and actors: GC and Sub")
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def write_fake_pdftoppm(
    root: Path,
    *,
    page_count: int,
    args_file: Path,
    name: str = "fake-pdftoppm.py",
) -> Path:
    path = root / name
    script = f"""#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
pathlib.Path({str(args_file)!r}).write_text(json.dumps(args))
prefix = pathlib.Path(args[-1])
for page in range(1, {page_count + 1}):
    pathlib.Path(f"{{prefix}}-{{page}}.png").write_bytes(b"png")
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def write_concurrency_codex(root: Path, state_file: Path) -> Path:
    path = root / "fake-codex-concurrency.py"
    script = f"""#!/usr/bin/env python3
import fcntl
import pathlib
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print("fake-codex-concurrency 1.0")
    raise SystemExit(0)

state_path = pathlib.Path({str(state_file)!r})
def update(delta):
    with state_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read().strip()
        current, maximum, total = [int(value) for value in raw.split(",")] if raw else [0, 0, 0]
        current += delta
        maximum = max(maximum, current)
        if delta > 0:
            total += 1
        handle.seek(0)
        handle.truncate()
        handle.write(f"{{current}},{{maximum}},{{total}}")
        handle.flush()
        fcntl.flock(handle, fcntl.LOCK_UN)

update(1)
time.sleep(0.15)
update(-1)
output = pathlib.Path(args[args.index("--output-last-message") + 1])
output.write_text("Visible text/OCR: bounded")
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


if __name__ == "__main__":
    unittest.main()
