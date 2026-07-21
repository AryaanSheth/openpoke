"""Email search task package."""

from .schemas import SEARCH_TOOL_NAME, TASK_TOOL_NAME, TaskEmailSearchPayload, get_schemas
from .tool import EmailSearchToolResult, GmailSearchEmail, build_registry, task_email_search

__all__ = [
    "GmailSearchEmail",
    "EmailSearchToolResult",
    "TaskEmailSearchPayload",
    "SEARCH_TOOL_NAME",
    "TASK_TOOL_NAME",
    "build_registry",
    "get_schemas",
    "task_email_search",
]
