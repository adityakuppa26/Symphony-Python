'''
Responsible for reading and rendering the workflow file. 
'''


from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import BaseModel

from .config import WorkflowConfig, load_config
from .models import Issue


class WorkflowError(Exception):
    """Raised for invalid WORKFLOW.md contents or prompt rendering failures."""


class WorkflowDefinition(BaseModel):
    path: Path
    raw_config: dict[str, Any]
    config: WorkflowConfig
    prompt_template: str


def load_workflow(path: str | Path, environ: Mapping[str, str] | None = None) -> WorkflowDefinition:
    workflow_path = Path(path).expanduser()
    if not workflow_path.exists():
        raise WorkflowError(f"Workflow file does not exist: {workflow_path}")
    text = workflow_path.read_text(encoding="utf-8")
    raw_config, body = split_front_matter(text)
    config = load_config(raw_config, workflow_path, environ=environ)
    return WorkflowDefinition(
        path=workflow_path.resolve(),
        raw_config=raw_config,
        config=config,
        prompt_template=body.strip(),
    )


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    close_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            close_index = index
            break
    if close_index is None:
        raise WorkflowError("YAML front matter is missing a closing ---")

    yaml_text = "\n".join(lines[1:close_index])
    try:
        loaded = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        raise WorkflowError(f"YAML front matter failed to parse: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise WorkflowError("YAML front matter must be a mapping")

    return loaded, "\n".join(lines[close_index + 1 :])


def render_prompt(
    workflow: WorkflowDefinition,
    issue: Issue,
    extra_context: Mapping[str, object] | None = None,
) -> str:
    env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
    try:
        template = env.from_string(workflow.prompt_template)
        context: dict[str, object] = {
            "issue": issue,
            "config": workflow.config,
            "workflow": workflow,
        }
        if extra_context:
            context.update(extra_context)
        return template.render(**context)
    except TemplateError as exc:
        raise WorkflowError(f"Prompt rendering failed: {exc}") from exc
