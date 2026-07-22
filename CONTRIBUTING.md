# Contributing

- Support Python 3.10 or newer and use only the standard library at runtime.
- Keep MCP stdout strictly newline-delimited UTF-8 JSON-RPC; diagnostics belong on stderr.
- Treat an MLT project as user data: write atomically, keep backups, and preserve unknown XML.
- Validate all external paths and subprocess arguments; never invoke a shell from the server.
- Keep tool handlers thin. Timeline rules belong in the project model, and platform/process
  behavior belongs in dedicated modules.
- Return recoverable user/input problems as tool errors without terminating the MCP server.
- Tests must use public interfaces and include literal expected outcomes.
- Add a failing regression test before each bug fix and keep real Shotcut integration opt-in.
- Run `ruff format --check .`, `ruff check .`, `mypy`, `python scripts/check_release.py`, and
  `python -B -m unittest discover -s tests -v` before publishing changes.
- Keep runtime and `manifest.json` versions aligned. `server.json` records the latest published
  artifact; the release workflow derives the next URL and checksum from the attached MCPB.
