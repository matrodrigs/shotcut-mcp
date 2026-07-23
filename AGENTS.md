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
tools -> platform, project, protocol, render
project -> missing_media, platform, project_document, project_snapshot, protocol, storage
missing_media -> platform, protocol
project_document -> media, mlt_xml
project_snapshot -> project_document, mlt_xml, path_policy
render -> platform, project_snapshot, protocol, render_jobs, storage
render_worker -> platform, render_jobs, storage
platform -> media, path_policy, processes, protocol, storage
media -> processes, protocol
path_policy -> mlt_xml
processes -> path_policy, protocol
```

Module ownership:

- `server.py`: JSON-RPC/MCP lifecycle, concurrency, cancellation, progress transport,
  and wire compatibility.
- `tools.py`: MCP tool catalog, schemas, annotations, and thin handler routing.
- `project.py`: public project-level orchestration and transaction workflow.
- `missing_media.py`: bounded missing-resource discovery, scoring, and visualization.
- `project_document.py`: MLT XML model, timeline invariants, and edit semantics.
- `project_snapshot.py`: read-only MCP projection and stable timing facts from an MLT document.
- `platform.py`: stable public orchestration interface for Shotcut and MLT operations.
- `path_policy.py`: canonical path resolution and embedded network-resource policy.
- `processes.py`: executable discovery and cancellable child-process supervision.
- `media.py`: FFprobe execution, caching, normalized media summaries, and bounded parsing
  of FFmpeg quality analyzers.
- `mlt_xml.py`: shared decoding of MLT properties and clock values.
- `render.py`: public durable render-job lifecycle.
- `render_worker.py`: out-of-process render ownership and final output promotion.
- `render_jobs.py`: private persistent job metadata and bounded logs.
- `storage.py`: locks, backups, revisions, and atomic output transactions.
- `protocol.py`: schema validation plus request-scoped cancellation and progress context.

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
- Treat Shotcut marker `end` values as exclusive. Translate a range marker to MLT's
  inclusive consumer `out` by subtracting one, and reject empty ranges.
- Reject media replacement next to a transition instead of attempting to rewire its
  nested tractor. Preserve the entry range and isolate a shared producer before mutation.
- Apply the configured allowed-root and network-resource policy to all user-controlled
  data paths. Executable discovery is not a data-path operation.
- Do not invoke a shell for Shotcut, MLT, FFmpeg, or FFprobe commands. Pass argument lists
  directly and terminate the process group on timeout or MCP cancellation.
- Restrict render consumer properties to single-file-safe options unless the
  administrator explicitly enables unsafe properties.
- Keep MCP stdout strictly newline-delimited UTF-8 JSON-RPC. Diagnostics go to stderr.
- Emit request progress only for a caller-supplied token, keep values strictly increasing,
  and omit the progress `message` for MCP `2024-11-05`. A render token ends when
  `start_render` returns; durable progress remains owned by `render_status`.
- Bound message size, in-flight work, logs, diffs, operation batches, and caches.

## Change practices

- Make the smallest cohesive change that solves the observed problem.
- Diagnose bugs as symptom -> source -> consequence -> remedy before editing.
- Prefer extraction along an existing responsibility seam over splitting by file size.
- Keep edit rules in `project_document.py` and read projections in
  `project_snapshot.py`; keep transaction and filesystem policy out of both.
- Keep OS/process mechanics in `processes.py`; callers should not duplicate subprocess
  construction, cancellation polling, or executable discovery.
- Keep FFmpeg analyzer commands and stderr parsers in `media.py`. Report a missing filter
  or inapplicable stream per analyzer so one unavailable check does not erase useful results.
- Keep path authorization in `path_policy.py`; do not add ad hoc prefix checks.
- Keep MCP handlers thin and validate through the declared tool schema before execution.
- Route progress through `protocol.py` and serialize notifications in `server.py`; domain
  modules report semantic milestones without knowing tokens or protocol revisions.
- Preserve Python 3.10 compatibility and standard-library-only runtime code.
- Use type annotations for new interfaces and return structured dictionaries consistent
  with existing tool results.
- Do not silently weaken validation, revision checks, backup creation, path policy, or
  output protection to make a test pass.

## Agent-facing MCP guidance

Treat the guidance exposed to an MCP client as part of the public interface, not as
secondary documentation. The server must give an agent enough runtime context to choose
the right tool, construct a valid call, interpret the result, and recover safely without
requiring the repository README.

- Keep `initialize.instructions`, tool names and descriptions, input and output schemas,
  annotations, capability details and examples, `manifest.json`, the plugin default
  prompt, `README.md`, and `docs/spec.md` aligned whenever the public behavior changes.
- Put the highest-value intent routing early in `initialize.instructions`, including the
  normal inspect, edit, visual-preview, validate, and render workflows. Keep it concise;
  do not paste the README into the protocol response.
- Write tool descriptions around user intent and explain when to use the tool. Clearly
  distinguish tools with similar purposes, such as project inspection versus visual
  review, exact preview frames versus contact sheets, and diagnostics versus capability
  discovery.
- Give every public parameter a useful description. Document units, accepted values,
  defaults, identifier provenance, revision requirements, and path or overwrite behavior
  where applicable. Examples must use values and fields accepted by the implementation.
- Keep `shotcut_capabilities` operation schemas and examples synchronized with the edit
  operations implemented by `project_document.py`; never advertise unsupported fields or
  enum values.
- Return structured results and actionable structured errors through declared output
  schemas when the negotiated MCP protocol version supports them. Preserve version
  gating so older clients do not receive newer protocol fields.
- For visual user intents such as "show me the edit," prefer a bounded managed preview or
  contact sheet that the client can display directly. Never read arbitrary user-selected
  output paths back into the MCP response.
- Prefer improving the existing deep tool interface and its guidance over adding shallow
  aliases for natural-language requests. Add a new tool only when it provides distinct
  behavior or materially reduces what callers must understand.
- Add or update tests that catch drift between handlers, schemas, capability examples,
  manifest descriptions, protocol versions, and the intended agent workflows. Test the
  observable MCP interface rather than private catalog construction details.

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
rendering, media analysis, executable discovery, process supervision, or compatibility checks:

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
- Release only an existing `vX.Y.Z` tag contained in `main`; its version must match the package
  metadata and a closed changelog section. Build and verify the MCPB before publishing the draft.

## Documentation and commits

- Update `README.md`, `docs/spec.md`, security guidance, and changelog when behavior or
  administrator policy changes.
- Document why an invariant exists, especially when it protects against a reproduced bug.
- Keep commits atomic and use focused messages such as `fix(render): ...`,
  `refactor(project): ...`, or `docs: ...`.
- Never commit generated caches, local paths, render artifacts, project backups, tokens,
  credentials, or machine-specific configuration.
