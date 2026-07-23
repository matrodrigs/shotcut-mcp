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
- Keep runtime, `manifest.json`, and the base version before `+` in
  `.codex-plugin/plugin.json` aligned. The plugin suffix is only a local-install cachebuster.
  `server.json` records the latest published artifact; the release workflow derives the next URL
  and checksum from the attached MCPB.

## Releasing

1. Update `shotcut_mcp.__version__`, `manifest.json`, and the base version before `+` in
   `.codex-plugin/plugin.json` to the same `X.Y.Z` version.
2. Close the matching `CHANGELOG.md` section with its release date and commit the changes to
   `main`.
3. Wait for the complete `CI` workflow on that exact `main` commit to succeed.
4. Create and push an annotated `vX.Y.Z` tag that points to that commit:

   ```bash
   git tag -a vX.Y.Z -m "Shotcut MCP X.Y.Z"
   git push origin vX.Y.Z
   ```

The tag workflow first requires a successful `main` push CI run for the exact tagged commit. It
then repeats the full static and unit checks, builds a deterministic MCPB from a strict runtime
allowlist, uploads it with its SHA-256 checksum to a draft release, downloads and verifies the
remote artifact, and publishes the release. It directly invokes the reusable Registry workflow
because GitHub does not emit recursive workflow runs for releases created with the repository
token. After Registry publication succeeds, it records the published URL and checksum in
`server.json` on `main`.

The same workflow can be dispatched manually for an existing, unpublished `vX.Y.Z` tag. It never
creates a release from an untagged commit and refuses tags whose commit is not contained in `main`
or lacks a successful CI run for that SHA.
