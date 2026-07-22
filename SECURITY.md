# Security policy

## Reporting a vulnerability

Use GitHub's private security-advisory reporting for vulnerabilities that could expose files,
execute unintended processes, escape configured roots, or overwrite user data. Do not include
sensitive reproduction data in a public issue. Use ordinary GitHub issues for non-sensitive bugs.

## Local trust boundary

Shotcut MCP is a local stdio server. Its client can request reads, project writes, previews,
process launches and renders with the permissions of the user running the server. Configure
`SHOTCUT_MCP_ALLOWED_ROOTS` and `SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS=1` when the client should see
only a bounded filesystem area.

Network resources and arbitrary consumer properties are disabled by default. Only administrators
should enable `SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES=1` or
`SHOTCUT_MCP_ALLOW_UNSAFE_CONSUMER_PROPERTIES=1`, after reviewing the project and output behavior.

Project revisions, locks and backups protect MCP writes; they do not coordinate unsaved Shotcut
GUI state. Save/close GUI edits before starting an MCP transaction.
