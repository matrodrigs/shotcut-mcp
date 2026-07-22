# AGENTS.md

This file defines the engineering practices for automated and human contributors to
Shotcut MCP. Apply these rules to the entire repository unless a more specific
`AGENTS.md` exists below the file being changed.

## Project intent

Shotcut MCP is a dependency-free Python MCP server that edits Shotcut MLT XML,
validates it with the installed MLT runtime, renders previews and final media, and
preserves user projects through transactional writes. Correctness and preservation of
user data take priority over convenience or clever abstractions.

The validated compatibility target is Shotcut 26.6.25 with MLT 7.40.x. Do not infer
that a successful MLT repository preflight proves an optional service loaded; RNNoise
must continue to be checked independently as both a link and a filter.

## Architecture and dependency direction

Keep modules deep: expose a small interface that hides substantial behavior. Add a seam
only when behavior genuinely varies or when it isolates an external dependency. Avoid
pass-through wrappers, speculative ports, and circular imports.

The intended dependency direction is:

```text
server -> protocol, tools
tools -> platform, project, render
project -> platform, project_document, project_snapshot, protocol, storage
project_document -> media, mlt_xml
project_snapshot -> project_document, mlt_xml, path_policy
render -> platform, protocol, render_jobs, storage
render_worker -> platform, render_jobs, storage
platform -> media, path_policy, processes, storage
media -> processes
processes -> path_policy, protocol
```

Module ownership:

- `server.py`: JSON-RPC/MCP lifecycle, concurrency, cancellation, and wire compatibility.
- `tools.py`: MCP tool catalog, schemas, annotations, and thin handler routing.
- `project.py`: public project transaction workflow: create, plan, edit, backup, restore.
- `project_document.py`: MLT XML model, timeline invariants, and edit semantics.
- `project_snapshot.py`: read-only MCP projection of an MLT document.
- `platform.py`: stable public orchestration interface for Shotcut and MLT operations.
- `path_policy.py`: canonical path resolution and embedded network-resource policy.
- `processes.py`: executable discovery and cancellable child-process supervision.
- `media.py`: FFprobe execution, caching, and normalized media summaries.
- `mlt_xml.py`: shared decoding of MLT properties and clock values.
- `render.py`: public durable render-job lifecycle.
- `render_worker.py`: out-of-process render ownership and final output promotion.
- `render_jobs.py`: private persistent job metadata and bounded logs.
- `storage.py`: locks, backups, revisions, and atomic output transactions.
- `protocol.py`: schema validation and request cancellation context.

Preserve the public imports from `project.py` and `platform.py`. Internal modules may be
reorganized without changing MCP clients, tool names, schemas, result shapes, or the
documented public imports exposed by those two modules.

## Non-negotiable safety invariants

- Treat every `.mlt`/`.xml` project and rendered output as user data.
- Never replace a project until the complete candidate passes MLT validation.
- Recheck the project revision after validation and before replacement.
- Use sibling temporary files, `fsync` where supported, and atomic replacement.
- Preserve an existing target if validation, preview, render, or promotion fails.
- Keep project backups isolated by canonical project identity. Restore only filenames
  produced by the backup store; directory membership alone is not ownership proof.
- Preserve unknown MLT XML elements and properties unless an operation explicitly owns
  them. Reject duplicate IDs and ambiguous timeline roots instead of guessing.
- Clone a shared producer before applying a clip-local mutation.
- Apply the configured allowed-root and network-resource policy to all user-controlled
  data paths. Executable discovery is not a data-path operation.
- Do not invoke a shell for Shotcut, MLT, FFmpeg, or FFprobe commands. Pass argument lists
  directly and terminate the process group on timeout or MCP cancellation.
- Restrict render consumer properties to single-file-safe options unless the
  administrator explicitly enables unsafe properties.
- Keep MCP stdout strictly newline-delimited UTF-8 JSON-RPC. Diagnostics go to stderr.
- Bound message size, in-flight work, logs, diffs, operation batches, and caches.

## Change practices

- Make the smallest cohesive change that solves the observed problem.
- Diagnose bugs as symptom -> source -> consequence -> remedy before editing.
- Prefer extraction along an existing responsibility seam over splitting by file size.
- Keep edit rules in `project_document.py` and read projections in
  `project_snapshot.py`; keep transaction and filesystem policy out of both.
- Keep OS/process mechanics in `processes.py`; callers should not duplicate subprocess
  construction, cancellation polling, or executable discovery.
- Keep path authorization in `path_policy.py`; do not add ad hoc prefix checks.
- Keep MCP handlers thin and validate through the declared tool schema before execution.
- Preserve Python 3.10 compatibility and standard-library-only runtime code.
- Use type annotations for new interfaces and return structured dictionaries consistent
  with existing tool results.
- Do not silently weaken validation, revision checks, backup creation, path policy, or
  output protection to make a test pass.

## Testing

Tests should cross the same public seam used by callers and assert observable outcomes.
Avoid tests that depend on private data structures or exact internal call ordering. Add a
regression test for every confirmed bug before or with its fix.

Fast local checks:

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -B -m compileall -q shotcut_mcp scripts tests
python -B -m unittest discover -s tests -v
python scripts/check_release.py
```

Run the real integration test when changing project generation, MLT validation, preview,
rendering, executable discovery, process supervision, or compatibility checks:

```powershell
$env:SHOTCUT_MCP_INTEGRATION = "1"
python -B -m unittest tests.test_integration -v
Remove-Item Env:SHOTCUT_MCP_INTEGRATION
```

The ordinary suite must remain independent of a local Shotcut installation. Real Shotcut
integration stays opt-in and must use temporary projects and outputs.

## Protocol and release compatibility

- Maintain compatibility with every protocol version declared in `server.py`.
- Older clients must not receive fields introduced only by newer MCP protocol versions.
- Notifications never receive JSON-RPC responses.
- Tool schemas, handlers, `manifest.json`, documentation, and release metadata must agree.
- Keep `shotcut_mcp.__version__` and `manifest.json` aligned.
- `server.json` describes the latest published artifact, not unreleased source state.
- Pin release tooling by version and checksum; never publish an unverified MCPB.

## Documentation and commits

- Update `README.md`, `docs/spec.md`, security guidance, and changelog when behavior or
  administrator policy changes.
- Document why an invariant exists, especially when it protects against a reproduced bug.
- Keep commits atomic and use focused messages such as `fix(render): ...`,
  `refactor(project): ...`, or `docs: ...`.
- Never commit generated caches, local paths, render artifacts, project backups, tokens,
  credentials, or machine-specific configuration.
