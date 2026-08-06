from __future__ import annotations

import fnmatch
from typing import Iterable, Mapping


DEFAULT_JIRA_ENVIRONMENT_EXCLUDE_PATTERNS = (
    "JIRA_*",
    "*_JIRA_TOKEN",
    "*_JIRA_EMAIL",
)


def filtered_subprocess_environment(
    source: Mapping[str, str],
    *,
    excluded_names: Iterable[str | None] = (),
    excluded_patterns: Iterable[str] = (),
) -> dict[str, str]:
    """Copy an environment while removing explicitly excluded variables."""

    names = frozenset(name for name in excluded_names if name)
    patterns = tuple(excluded_patterns)
    return {
        name: value
        for name, value in source.items()
        if name not in names
        and not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
    }
