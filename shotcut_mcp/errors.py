class ToolError(Exception):
    """A recoverable error returned to the MCP caller."""


class ConflictError(ToolError):
    """The project changed after the caller inspected it."""
