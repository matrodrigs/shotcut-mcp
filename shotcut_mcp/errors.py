class ToolError(Exception):
    """A recoverable error returned to the MCP caller."""


class ConflictError(ToolError):
    """The project changed after the caller inspected it."""


class RequestCancelled(ToolError):
    """The MCP client cancelled an in-flight request."""
