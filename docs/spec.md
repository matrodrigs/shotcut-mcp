# Shotcut MCP robust editor specification

## Goal

Provide a fast, local and reliable MCP server that can create, inspect, edit, validate,
preview and render saved Shotcut 26.6 projects without requiring a network service.

## Required behavior

- Preserve unknown MLT XML elements, attributes and properties when editing.
- Apply many edits in one parse/write transaction.
- Guard writes with a revision hash and an MCP lock file.
- Recheck the on-disk revision after validating the temporary project and before replacement.
- Create a timestamped backup in an isolated per-project namespace before every replacement.
- Never overwrite a project, media file or render output without an explicit flag.
- Render previews and exports to protected sibling files and promote them atomically only if the
  original target has not changed.
- Supervise renders outside the MCP stdio process so completion and cancellation survive restart.
- Never infer export authorization from editing, validation, preview, or visual-review intent. Treat
  an explicit export request in the active task as approval; otherwise present project, output,
  preset, range/duration, and overwrite behavior and wait for approval without asking twice.
- After inspection and committed edits, guide callers to surface concise result summaries and a
  managed contact sheet or exact preview when the changes are visual.
- Return opaque `item_ref` values for clip and transition occurrences, scope them to the inspected
  project revision, and resolve them against item identity throughout one sequential edit batch.
  Preserve legacy track/index selectors and support bounded transaction-local aliases for newly
  created or split items without writing private identity metadata into MLT XML.
- Support video and audio tracks, gaps, clips, same-track or all-unlocked ripple/non-ripple trim,
  roll, slip, slide, constant timewarp, same-direction forward or reverse timeremap speed maps,
  split, move, ripple/overwrite edits,
  crossfades, generic MLT filters, structured clip opacity, semantic pan/zoom/rotation/volume
  animation, native keyframed properties,
  text/color/tone generators,
  markers, project notes, subtitle feeds, media relinking, clip duplication, safe source
  replacement, filter ordering, and marker updates.
- Expose MLT service discovery so callers can use filters, transitions and links installed with
  the user's Shotcut build instead of relying on a hard-coded catalog.
- Provide project inspection and durable render-job management.
- Return stable, structured JSON from every tool and use English for public tool descriptions,
  server instructions and error messages.
- Return tool execution failures with `isError`, a stable machine-readable error code,
  recoverability, recommended action/tool, and bounded structured details while preserving the
  legacy human-readable error fields. Publish the error alternative in every current output schema;
  keep malformed requests and invalid arguments as JSON-RPC errors with equivalent recovery data.
- Use distinct recovery families when the caller's next safe action changes, including project
  restoration, render-history restart, media probing, search-root correction, compatibility
  diagnosis, visual fallback, and reporting corrupt durable render state. Preserve successful
  missing-media diagnosis data when only its optional visualization fails.
- Route common user intents through concise server instructions, including visual review,
  optimistic-conflict recovery, missing media, color diagnosis, export monitoring and backups.
- Keep the first 512 characters of server instructions self-contained with saved-state and normal
  edit safety. Keep the Codex interface to at most three one-line starter prompts of at most 128
  characters; operational policy belongs in server instructions and tool contracts.
- When the user mentions recent GUI edits or an open Shotcut session, explain that the MCP sees
  only the project saved on disk, ask them to save first, and avoid concurrent saves.
- When dimensions or frame rate were not requested for a new project, guide callers to probe
  representative source media before choosing the profile instead of treating defaults as intent.
- Validate tool arguments against the published input contracts, including every operation's
  focused `shotcut_capabilities` schema, revision-or-explicit-force guards for edits and restores,
  and mutually exclusive full/range/marker render modes.
- Publish required stable result fields and typed nested collections for every tool output,
  including the complete project-inspection shape, for structured-content protocol revisions;
  omit output schemas for legacy clients that predate structured tool results. On current
  revisions, keep the text content concise instead of duplicating the complete structured payload.
- Return the full catalog, presets, compatibility, and workflow from an unfiltered
  `shotcut_capabilities` call; when `operation` is supplied, return only that operation's complete
  schema, example, and transaction guarantees. Enforce that same operation schema before planning
  or applying a batch.
- Propagate MCP cancellation notifications to subprocess-backed operations.
- Apply the configured message budget to both incoming and outgoing newline-delimited JSON-RPC.
  Reject an oversized serialized project candidate before backup or replacement, using the same
  limit enforced while loading a project. Default to 128 MiB and allow administrators to configure
  a value clamped between 1 MiB and 512 MiB.
- Provide a read-only plan/diff operation before transactional edits.
- Render bounded preview batches and atomically promoted contact sheets at exact frames.
- Allow single previews and contact sheets to use bounded server-owned output when the caller does
  not need a persistent destination, and embed small review images within the MCP message budget.
- Normalize source/project color metadata and own SDR/HLG/PQ project annotations as one semantic edit.
- Smoke-test hardware encoders instead of trusting FFmpeg's advertised encoder list.
- Persist bounded progress samples, elapsed time, ETA inputs, terminal metrics, and paginated history.
- Guide callers to poll durable render status to a terminal state and surface only meaningful
  status, progress, or ETA changes instead of unchanged polls or raw log spam.
- Diagnose missing media with bounded Shotcut-hash/basename search and require an explicit relink edit.
- Analyze media quality with independently bounded FFmpeg silence, black, freeze, interlace,
  and EBU R128 loudness checks, returning partial structured results when a filter or stream is
  unavailable.
- Report installed FFmpeg analyzer availability in the compatibility doctor without making an
  optional analyzer part of the core Shotcut/MLT compatibility verdict.
- Validate project readiness through one public operation that combines local-resource and
  required-service checks with first-frame MLT processing. Report `valid` for successful
  first-frame Melt processing and `ready` only when those dependency checks also pass; do not
  direct callers to repeat it after every already-validated edit.
- Render exactly one of the complete project, one explicit inclusive frame range, or one non-empty
  Shotcut range marker while preserving the durable render-job lifecycle. Accept at most 50
  scalar advanced consumer properties and keep the default safe single-file allowlist.
- Capture exact project bytes and SHA-256 revision for every render mode, accept an optional
  `expected_revision`, and make the worker consume only a uniquely named sibling snapshot so
  relative resources retain their original base and live-project edits cannot change the job.
- Retain the verified successful snapshot as the exact editable project artifact. Return it with
  the rendered media in structured `artifacts`; add `resource_link` content only for MCP
  `2025-06-18` and `2025-11-25`, while legacy revisions receive both paths in text content.
- Export point markers in Shotcut's chapter text format, with opt-in range markers and marker-color
  filtering, through the same atomic output protection used by other generated files.
- Emit strictly increasing request-scoped MCP progress notifications only when the caller provides
  a token. Keep durable post-return render progress in `render_status`.

## Compatibility boundary

- Target Shotcut 26.6.25 with MLT 7.40.0 serialization in the compatible MLT 7.40.x family.
- Warm and retry cold MLT repository initialization with progressively longer 5, 10, and 20 second
  attempts before status, validation, preview and rendering. Keep every installed service
  available and cache readiness by executable and MLT environment identity.
- Check RNNoise link/filter availability separately from the repository preflight. Prefer the
  latency-safe MLT 7.40 `link` service when callers construct RNNoise processing.
- Work on saved `.mlt`/MLT XML projects. Unsaved GUI state is out of scope.
- Preserve unsupported structures, but reject an edit when a target is ambiguous or when
  modifying it would require guessing about an unknown transition layout.
- Preserve Shotcut's exclusive marker end convention and translate it to MLT's inclusive render
  `out` value. Use MLT's still-image producer for durationless image streams so trims, splits and
  source replacement preserve their timeline ranges. Reject source replacement next to transitions
  rather than guessing at tractor rewiring.
- Generic filters accept native MLT properties; the MCP does not promise that every third-party
  filter is available or renderable on every machine. Structured clip opacity owns and reuses one
  brightness filter, keeps its RGB level neutral, and animates only alpha.
- `animate_clip` compiles normalized creative keyframes to MCP-owned Shotcut 26.6 filters: affine
  rectangle/rotation for transform, neutral-brightness alpha for opacity, and volume level in dB.
  Reapplying it reuses only those owned filters and leaves user-created filters untouched.
- Deny network resources and sidecar/path-bearing consumer properties by default. Administrators
  may opt in through environment policy and may constrain every tool path to canonical roots.
- Apply those policies to every recognized MLT path representation, including timewarp, proxy,
  luma, source, filename, and filter resources.
- Keep HDR display preview, mixed HLG/PQ conversion, zero-crossing speed maps, and ambiguous
  third-party chain edits out of the verified interface. Reverse speed maps must remain entirely
  negative and start from the selected source range's final frame.

## Verification

- MCP negotiation, schema-validation, batching and cancellation tests.
- Protocol-version tests for token-scoped, monotonic progress notifications.
- Unit tests through public project, preview and render APIs.
- Regression tests for preservation, optimistic concurrency, backup ownership, atomic output,
  shared producers, advanced timeline edits, speed/color annotations, assisted relinking,
  bounded process/log output, render history/ETA, orphan cleanup and security policies.
- Real ffmpeg/ffprobe/melt integration covering multitrack creation, stable item references,
  semantic transform/audio animation, locked-track ripple editing,
  forward/reverse timeremap validation, still-image trims and replacement, opacity composition,
  preview, media-quality analysis, range rendering, and final render.
- Manifest/version/tool-catalog validation plus Ruff and Mypy in Windows, macOS, and Linux CI.
  Real Shotcut/MLT integration remains opt-in. Release tags
  must point to a `main` commit whose exact SHA completed that CI successfully.
