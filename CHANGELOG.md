# Changelog

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
