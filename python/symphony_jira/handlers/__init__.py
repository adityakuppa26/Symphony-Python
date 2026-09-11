"""Development workflow policy and repository scope."""

from ..config import WorkflowConfig
from .development import DevelopmentHandler


def workflow_handler(config: WorkflowConfig) -> DevelopmentHandler:
    return DevelopmentHandler(config)
