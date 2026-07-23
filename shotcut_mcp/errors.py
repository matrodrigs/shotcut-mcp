class ToolError(Exception):
    """A recoverable error returned to the MCP caller."""


class ConflictError(ToolError):
    """The project changed after the caller inspected it."""

    def __init__(
        self,
        message: str,
        *,
        expected_revision: str | None = None,
        current_revision: str | None = None,
        recommended_action: str = "inspect_project",
    ) -> None:
        super().__init__(message)
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        self.recommended_action = recommended_action


class RequestCancelled(ToolError):
    """The MCP client cancelled an in-flight request."""
