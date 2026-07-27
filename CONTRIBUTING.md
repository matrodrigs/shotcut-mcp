# Contributing

Start with the [architecture overview](docs/architecture.md) for the repository layout and module
responsibilities, and consult the [behavioral specification](docs/spec.md) for compatibility
boundaries and verified behavior.

- Support Python 3.10 or newer and use only the standard library at runtime.
- Keep MCP stdout strictly newline-delimited UTF-8 JSON-RPC; diagnostics belong on stderr.
- Treat an MLT project as user data: write atomically, keep backups, and preserve unknown XML.
- Validate all external paths and subprocess arguments; never invoke a shell from the server.
- Keep tool handlers thin. Timeline rules belong in the project model, and platform/process
  behavior belongs in dedicated modules.
- Follow the dependency direction in `docs/architecture.md`; preserve `project.py` and
  `platform.py` as the stable public seams instead of importing private mechanics into handlers.
- Keep operation selector/alias semantics in `project_document.py` and derive their public schema
  projection in `tools.py`. Do not maintain a second operation-name list for the same behavior.
- Keep validated Shotcut/MLT versions in `shotcut_mcp/__init__.py`; release checks verify that
  `AGENTS.md`, `README.md`, `docs/spec.md`, and the project website contain those values.
- Return recoverable user/input problems as tool errors without terminating the MCP server.
- Tests must use public interfaces, include literal expected outcomes, and be named for their
  domain seam (`project`, `protocol`, `platform`, `media`, or `render`) rather than release age.
- Add a failing regression test before each bug fix and keep real Shotcut integration opt-in.
- After changing the runtime tool catalog, keep the README table's concise human summaries current,
  then run `python scripts/check_release.py --sync-tool-contracts` to refresh the mechanical
  `manifest.json` descriptions and website tool counts.
- Install the pinned development tools from `requirements-dev.txt`, then run
  `python -B scripts/check_ci.py all` before publishing changes. On Windows, this command runs
  the full unit suite through a temporary drive alias so canonical-path behavior matches CI.
  Enable the versioned pre-push gate once per clone with
  `git config core.hooksPath .githooks`.
- Keep runtime, `manifest.json`, `.claude-plugin/plugin.json`, and the base version before `+` in
  `.codex-plugin/plugin.json` aligned. Keep both client adapters pointed at
  `scripts/shotcut_mcp_server.py`, and keep `.claude-plugin/marketplace.json` aligned with the
  Claude manifest; the Codex plugin suffix is only a local-install cachebuster.
  `server.json` records the latest published artifact; the release workflow derives the next URL
  and checksum from the attached MCPB.

## Releasing

1. Update `shotcut_mcp.__version__`, `manifest.json`, `.claude-plugin/plugin.json`, and the base
   version before `+` in `.codex-plugin/plugin.json` to the same `X.Y.Z` version.
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
