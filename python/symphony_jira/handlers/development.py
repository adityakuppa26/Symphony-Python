from .base import WorkflowHandler


class DevelopmentHandler(WorkflowHandler):
    scope_instructions = (
        "This is the development workflow. Inspect Jira requirements alongside "
        "the application code, plan the smallest correct change, and implement "
        "only the approved scope before independent code review and handoff."
    )
