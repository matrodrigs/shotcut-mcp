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
- Support video and audio tracks, gaps, clips, explicit ripple/non-ripple trim, roll, slip,
  slide, constant timewarp, positive timeremap speed maps, split, move, ripple/overwrite edits,
  crossfades, generic MLT filters, keyframed properties, text/color/tone generators,
  markers, project notes, subtitle feeds, media relinking, clip duplication, safe source
  replacement, filter ordering, and marker updates.
- Expose MLT service discovery so callers can use filters, transitions and links installed with
  the user's Shotcut build instead of relying on a hard-coded catalog.
- Provide project inspection and durable render-job management.
- Return stable, structured JSON from every tool and use English for public tool descriptions,
  server instructions and error messages.
- Route common user intents through concise server instructions, including visual review,
  optimistic-conflict recovery, missing media, color diagnosis, export monitoring and backups.
- Validate tool arguments against the published input schemas and shape responses for the
  negotiated MCP protocol revision.
- Publish output schemas for structured-content protocol revisions and omit them for legacy
  clients that predate structured tool results.
- Propagate MCP cancellation notifications to subprocess-backed operations.
- Provide a read-only plan/diff operation before transactional edits.
- Render bounded preview batches and atomically promoted contact sheets at exact frames.
- Allow single previews and contact sheets to use bounded server-owned output when the caller does
  not need a persistent destination, and embed small review images within the MCP message budget.
- Normalize source/project color metadata and own SDR/HLG/PQ project annotations as one semantic edit.
- Smoke-test hardware encoders instead of trusting FFmpeg's advertised encoder list.
- Persist bounded progress samples, elapsed time, ETA inputs, terminal metrics, and paginated history.
- Diagnose missing media with bounded Shotcut-hash/basename search and require an explicit relink edit.
- Analyze media quality with independently bounded FFmpeg silence, black, freeze, interlace,
  and EBU R128 loudness checks, returning partial structured results when a filter or stream is
  unavailable.
- Render either the complete project, one explicit inclusive frame range, or one non-empty
  Shotcut range marker while preserving the durable render-job lifecycle.
- Export point markers in Shotcut's chapter text format, with opt-in range markers and marker-color
  filtering, through the same atomic output protection used by other generated files.
- Emit strictly increasing request-scoped MCP progress notifications only when the caller provides
  a token. Keep durable post-return render progress in `render_status`.

## Compatibility boundary

- Target the installed Shotcut 26.6.25 and MLT 7.40.0 formats.
- Warm and retry cold MLT repository initialization before validation, preview and rendering,
  while keeping every installed service available and caching readiness by executable and MLT
  environment identity.
- Check RNNoise link/filter availability separately from the repository preflight. Prefer the
  latency-safe MLT 7.40 `link` service when callers construct RNNoise processing.
- Work on saved `.mlt`/MLT XML projects. Unsaved GUI state is out of scope.
- Preserve unsupported structures, but reject an edit when a target is ambiguous or when
  modifying it would require guessing about an unknown transition layout.
- Preserve Shotcut's exclusive marker end convention and translate it to MLT's inclusive render
  `out` value. Reject source replacement next to transitions rather than guessing at tractor rewiring.
- Generic filters accept native MLT properties; the MCP does not promise that every third-party
  filter is available or renderable on every machine.
- Deny network resources and sidecar/path-bearing consumer properties by default. Administrators
  may opt in through environment policy and may constrain every tool path to canonical roots.
- Apply those policies to every recognized MLT path representation, including timewarp, proxy,
  luma, source, filename, and filter resources.
- Keep HDR display preview, mixed HLG/PQ conversion, reverse/zero-crossing speed maps, and
  ambiguous third-party chain edits out of the verified interface.

## Verification

- MCP negotiation, schema-validation, batching and cancellation tests.
- Protocol-version tests for token-scoped, monotonic progress notifications.
- Unit tests through public project, preview and render APIs.
- Regression tests for preservation, optimistic concurrency, backup ownership, atomic output,
  shared producers, advanced timeline edits, speed/color annotations, assisted relinking,
  bounded process/log output, render history/ETA, orphan cleanup and security policies.
- Real ffmpeg/ffprobe/melt integration covering multitrack creation, editing, validation,
  preview, media-quality analysis, range rendering, and final render.
- Manifest/version/tool-catalog validation plus Ruff and Mypy in cross-platform CI. Release tags
  must point to a `main` commit whose exact SHA completed that CI successfully.
