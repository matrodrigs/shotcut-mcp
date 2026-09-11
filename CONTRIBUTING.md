# Contributing

Start with the [architecture overview](docs/architecture.md) for the repository layout and module
responsibilities, and consult the [behavioral specification](docs/spec.md) for compatibility
boundaries and verified behavior.

Follow [AGENTS.md](AGENTS.md) for engineering practices, safety invariants, and criteria for
running real integration. Runtime code supports Python 3.10+ and uses only the standard library.

## Development checks

```bash
python -m pip install -r requirements-dev.txt
git config core.hooksPath .githooks
python -B scripts/check_ci.py all
```

The shared runner checks types, formatting, dead code, tests, and release contracts. On Windows
it uses a temporary drive alias to exercise canonical paths. Real Shotcut integration is opt-in
locally and required by CI; use temporary media and outputs.

Tests should exercise public interfaces and literal expected outcomes. Name them for the domain
seam they cover, and add a failing regression test before each bug fix.

After changing the tool catalog, update the [tool reference](docs/reference.md#mcp-tools) and run
`python -B scripts/check_release.py --sync-tool-contracts` to refresh manifest descriptions and
website tool counts. For runtime-version changes, follow the compatibility procedure in AGENTS.md.

## Releasing

1. Update `shotcut_mcp.__version__`, `manifest.json`, `.claude-plugin/plugin.json`, and the base
   version before `+` in `.codex-plugin/plugin.json` to the same `X.Y.Z` version; keep the Claude
   marketplace entry aligned. `server.json` continues to describe the latest published artifact.
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
