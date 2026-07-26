from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """An expected tool execution failure returned to the MCP caller."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_error",
        recoverable: bool = True,
        recommended_action: str = "review_error_and_correct_request",
        recommended_tool: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.recommended_action = recommended_action
        self.recommended_tool = recommended_tool
        self.details = dict(details or {})


class ConflictError(ToolError):
    """Protected project or output state changed or is currently locked."""

    def __init__(
        self,
        message: str,
        *,
        expected_revision: str | None = None,
        current_revision: str | None = None,
        code: str = "project_revision_conflict",
        recommended_action: str = "inspect_project",
        recommended_tool: str | None = "inspect_project",
        details: dict[str, Any] | None = None,
    ) -> None:
        context = dict(details or {})
        if expected_revision is not None:
            context["expected_revision"] = expected_revision
        if current_revision is not None:
            context["current_revision"] = current_revision
        super().__init__(
            message,
            code=code,
            recommended_action=recommended_action,
            recommended_tool=recommended_tool,
            details=context,
        )
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class RequestCancelled(ToolError):
    """The MCP client cancelled an in-flight request."""

    def __init__(self, message: str = "Request cancelled.") -> None:
        super().__init__(
            message,
            code="request_cancelled",
            recommended_action="retry_if_still_requested",
        )
