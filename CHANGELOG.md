# Changelog

## Unreleased

### Added

- Report per-analyzer FFmpeg availability in `shotcut_doctor` and combine local-resource,
  required-service, and first-frame Melt checks in `validate_project`.
- Support wholly reverse timeremap speed maps and all-unlocked cross-track ripple trims that
  preserve locked tracks and marker timing.

### Changed

- Add Shotcut 26.6 compatibility fixtures and real MLT integration coverage for reverse speed maps
  and locked-track ripple behavior.
- Clarify saved-state, edit, readiness, review and recovery guidance; distinguish `valid` from
  `ready`, avoid redundant post-edit checks, and keep Codex starter prompts within directory limits.

## 1.4.2 (2026-07-26)

### Added

- Add parallel Codex and Claude Code plugin adapters plus a self-hosted Claude marketplace catalog
  over the same client-neutral MCP stdio server.
- Add a structured `set_clip_opacity` edit operation that accepts clip-relative keyframes,
  preserves RGB levels, and reuses one MCP-owned filter.

### Changed

- Document client-specific registration commands while preserving MCPB as the portable release
  artifact and validate both adapters against one version and server entry point.

### Fixed

- Use MLT's still-image producer when adding or replacing durationless image media so PNG and JPEG
  clips continue rendering after trims, splits, and source replacement.
- Animate only filter alpha for structured opacity edits, avoiding the color darkening caused by
  attenuating both brightness and alpha during composition.

## 1.4.1 (2026-07-25)

### Changed

- Clarify that Shotcut MCP reads only the project saved on disk, prompting callers to save recent
  GUI edits first and avoid concurrent saves.
- Guide callers to probe representative source media before choosing a new project's dimensions
  or frame rate when the user did not request them.

### Fixed

- Retry transient Windows sharing violations while reading durable render-job metadata so
  `render_status` and `cancel_render` remain reliable during atomic worker updates.

## 1.4.0 (2026-07-23)

### Changed

- Focus operation-specific `shotcut_capabilities` responses on the requested schema, example, and
  transaction guarantees; enforce those same schemas before planning or applying edits.
- Publish required stable fields and typed nested collections for every tool output, including
  identifiers, indexes, units, validation results, media streams, render jobs, and project state.

### Fixed

- Require either `expected_revision` or explicitly authorized `force: true` for edits and backup
  restores, while documenting the distinct plan and chapter-export revision contracts.
- Reject incomplete or conflicting render ranges and structurally invalid advanced consumer
  properties at the MCP schema seam.
- Align operation schemas with non-zero trim deltas, non-zero bounded clip speeds, marker-update
  requirements, and Shotcut's exclusive marker end convention.
- Correct `validate_project` output to expose `return_code` and nullable `diagnostic` fields instead
  of the nonexistent `validator` field.

## 1.3.1 (2026-07-22)

### Added

- Agent-facing MCP workflow instructions, described input parameters, operation-specific schemas
  and examples, structured output contracts, recoverable conflict context, and bounded inline
  preview images.
- Structured FFmpeg analysis for silence, black frames, frozen video, interlacing, and EBU R128
  loudness with bounded partial results.
- Full-project, inclusive-frame-range, and Shotcut range-marker renders plus atomic marker chapter
  exports.
- Transactional clip duplication, safe source replacement, filter reordering, marker updates, and
  token-scoped MCP progress notifications.

### Changed

- Single-frame previews and contact sheets can use bounded server-managed output when callers omit
  a destination path, and local-only tools now advertise closed-world behavior.

### Fixed

- Retry transient Windows sharing violations when atomically updating render-job state, and record
  supervisor initialization failures instead of leaving jobs stuck as running.
- Compare Windows media paths canonically, wait for durable render-job states in lifecycle tests,
  and block releases unless the exact tagged `main` commit has passed CI.

## 1.2.0 (2026-07-22)

### Added

- Exact-frame preview batches, atomic contact sheets, hardware-encoder smoke detection, render
  ETA/history, and bounded visual missing-media candidate sheets.
- Semantic SDR/HLG/PQ project workflows, constant timewarp, positive timeremap speed maps, and
  roll/slip/slide plus explicit same-track ripple/non-ripple trim operations.
- Normalized source color metadata and bounded Shotcut-hash/basename missing-media diagnosis.

### Fixed

- Apply allowed-root and network policy to media edits and all recognized embedded MLT resources.
- Use Shotcut 26.6's canonical `Native8Cpu` processing mode and maintain Shotcut hashes on relink.
- Bound project files, subprocess output, and render logs; reap finished supervisors without polling.

## 1.1.0 (2026-07-22)

### Added

- `shotcut_doctor` for Shotcut 26.6.25 / MLT 7.40.x, repository, RNNoise and policy checks.
- `plan_project_edit` for validated read-only snapshots and bounded XML diffs.
- Restart-resilient render supervision, durable cancellation and automatic output promotion.
- MCP input-schema validation, version-shaped tool schemas/results, 2025-03 batching and request
  cancellation, lifecycle enforcement and bounded request resources.
- Canonical allowed-root, absolute-path, network-resource and unsafe-consumer policies.
- Cross-platform CI with Ruff, Mypy, metadata checks and pinned release tooling.

### Changed

- Split project transactions, MLT document inspection, path policy, process supervision,
  and media probing into focused modules while preserving the public MCP interface.
- Added repository-wide engineering guidance for architecture, safety, testing, and releases.

### Fixed

- Prevent a project save made during MLT validation from being overwritten.
- Isolate similarly named projects' backups and enforce exact restore ownership.
- Prevent previews from overwriting their source project and preserve existing output on failure.
- Prevent arbitrary consumer properties from writing sidecar files outside the render target.
- Clone shared producers before clip-local filter edits.
- Remove unreferenced generated services and reject duplicate IDs or ambiguous main tractors.
- Report project filters, MLT links and technical color resources accurately.
- Include the MLT repository environment in cold-start and service cache identities.

### Security notes

- Network resources embedded in MLT XML are denied by default.
- Custom render properties are restricted to single-file outputs by default.
- Preview/render promotion detects concurrent target changes and uses sibling atomic replacement.
- Job state and backup storage use per-user/private directories where supported.
- Backup restores reject unrecognized files even when they are injected into the private namespace.
