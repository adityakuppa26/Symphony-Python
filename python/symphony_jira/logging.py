from __future__ import annotations

import logging
from collections.abc import Iterable


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def redact_text(text: str | None, secret_values: Iterable[str | None]) -> str | None:
    if text is None:
        return None
    redacted = text
    for value in secret_values:
        if value and len(value) >= 4:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted
