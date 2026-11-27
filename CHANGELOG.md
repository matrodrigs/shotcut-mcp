# Changelog

## Unreleased

### Fixed

- Fix “Connection closed” when starting the Claude Code plugin from another workspace on Windows (#10).
- Preserve changes made by another program while an edit or backup is in progress, including deletion of the project. The edit confirmation identifies the revision that was saved.
- Preserve an export destination created by another program just before the export finishes. A failed save releases the project lock, and only fully written backups appear for recovery.
- Clean up temporary preview and export files on Windows when the source or destination is read-only.
- Respect configured access restrictions when inspecting media, using network paths, opening projects in Shotcut, and accessing backups. Report unreadable project XML before starting Shotcut or Melt.
- Honor cancellation during diagnostics and media inspection. On Windows, cancelling a command also stops its child processes.
- Fix a crash when creating a review clip with MLT 7.42.0.

## 1.6.0 (2026-10-17)

### Added

- Find video, audio, and image files in a folder or its subfolders. Optional metadata shows which files could be inspected and which failed.
- Preview up to 30 seconds of motion and audio before exporting.
- Check whether Python and Shotcut are ready before connecting a client with `scripts/shotcut_mcp_server.py --check`.

### Changed

- Inspect a large timeline by track or frame range, request a summary, or browse results a page at a time. Inspection reports if the project changes between pages.
- Run up to two background renders at once by default. Administrators can set the limit from 1 to 8 with `SHOTCUT_MCP_MAX_ACTIVE_RENDERS`.

### Fixed

- Return the saved revision after creating, editing, or restoring large projects so the client can confirm success.
- Correct the instructions for replacing a clip's source media.
- Keep managed previews inside the configured folders, including when a folder is redirected.
- Prevent simultaneous edits from overwriting each other during recovery of an abandoned project lock.

## 1.5.6 (2026-09-12)

### Changed

- Generate batched preview frames and contact sheets in one render, including repeated frames in the requested order.
- Reuse recent successful tool and effect queries to speed up repeated checks.
- Use less memory when showing the differences for a planned edit to a large project.

## 1.5.5 (2026-09-11)

### Changed

- Create new projects in the Shotcut 26.8.1 / MLT 7.41.0 format and preserve the format recorded in existing projects.
- Show a warning for an untested Shotcut/MLT combination and report a failure when a required tool or service fails its checks.

### Fixed

- Export chapters when imported markers share a frame and one has no identifier.
- Report unreadable media metadata and invalid saved render status with recovery guidance.

## 1.5.4 (2026-09-07)

### Fixed

- Reduce the time needed to prepare edits on large timelines while preserving the order of existing timeline elements.

## 1.5.3 (2026-09-05)

### Fixed

- Preserve a project created by another program while a new project is being prepared. When overwrite is authorized, back up the file being replaced.
- Keep surviving clip references usable after adding or removing a transition. Allow updating a track while keeping its name.
- Preserve source frames and requested speed precision when replacing a speed ramp, including after trimming or splitting a clip.
- Keep the client connected after a malformed cancellation request and allow individual calls in older batch requests to be cancelled.
- Continue reporting render progress after an unusually long log line.
- Apply network restrictions to remote file URLs and Windows network paths written with mixed separators.
- Preserve colors in PNG previews of speed-mapped clips on Windows.

### Changed

- Explain when an older speed map lacks the source bounds needed for replacement. Reject reverse ramps that land on frame zero and can produce an invalid image. Constant-speed reversal remains available.
- Allow the assistant to choose an unused project filename in a supplied folder and reuse authorization already given for the same action.

## 1.5.2 (2026-09-05)

### Fixed

- Start background renders when the MCP client opens outside the extension directory. Relative media paths continue to resolve from the client's working directory, and startup failures include diagnostics (#3).
- Prevent Windows render-status checks from stopping a running render (#3).
- Allow Claude Desktop to install the MCPB when a supported Python version is present (#2).

## 1.5.1 (2026-08-02)

### Added

- Deliver the rendered media together with the exact editable Shotcut project used to produce it.

### Changed

- Launch the MCPB with the appropriate Python executable on Windows, macOS, and Linux.
- Ask for export approval when the request covers editing or preview only. An explicit export request already supplies that approval.

### Fixed

- Render projects, frame ranges, and markers from a saved snapshot so later edits cannot change the export.

## 1.5.0 (2026-07-27)

### Added

- Refer to the same clip throughout a batch of edits, including after earlier operations move it. Newly created clips can also receive a name for use later in the batch.
- Animate pan, zoom, rotation, opacity, and volume with clip-relative keyframes using `animate_clip`.
- Show specific recovery actions for problems such as missing media streams, unavailable preview frames, and stale render-history pages.

### Changed

- Give Shotcut's effects and codecs more time to load on a cold start.
- Open projects up to 128 MiB by default. Administrators can configure a limit from 1 to 512 MiB with `SHOTCUT_MCP_MAX_PROJECT_BYTES`.
- Reduce the size of large responses sent to MCP clients that support structured results.

### Fixed

- Keep oversized responses and progress updates within the configured connection limit.
- Reject edits that would make a project too large for the server to reopen.

## 1.4.5 (2026-07-26)

### Added

- Show which FFmpeg analyzers are available. Project validation checks local media, required effects and services, and whether Melt can process the first frame.
- Create fully reversed speed ramps and ripple trims across unlocked tracks while preserving locked tracks and marker timing.

## 1.4.2 (2026-07-26)

### Added

- Install Shotcut MCP as a Codex or Claude Code plugin, including through the Claude Code marketplace.
- Set or animate clip opacity with clip-relative keyframes.

### Fixed

- Keep PNG and JPEG clips renderable after trimming, splitting, or replacing the source.
- Preserve the brightness of a clip when animating its opacity over another track.

## 1.4.1 (2026-07-25)

### Changed

- Prompt users to save recent edits in Shotcut before working with the project on disk.
- Guide the assistant to inspect source media before choosing project dimensions or frame rate when the user leaves them unspecified.

### Fixed

- Keep render status and cancellation available when Windows briefly locks the job's status file.

## 1.4.0 (2026-07-23)

### Changed

- Show the accepted parameters and a working example when requesting help for a specific edit operation.

### Fixed

- Require the current project revision or explicit permission to override it before editing or restoring a backup.
- Reject conflicting render ranges, invalid custom export settings, and zero values for trim changes or clip speeds.
- Validate marker updates using Shotcut's exclusive end frame.
- Return Melt's exit code and diagnostic message from project validation.

## 1.3.1 (2026-07-22)

### Added

- Show preview images directly in supported MCP clients.
- Analyze silence, black frames, frozen video, interlacing, and EBU R128 loudness with FFmpeg.
- Export a full project, a frame range, or a named range marker. Export markers as a chapter list.
- Duplicate clips, replace their source media, reorder filters, and update markers in a single edit. Clients can request progress updates during operations.

### Changed

- Generate single-frame previews and contact sheets without requiring an output filename.

### Fixed

- Keep render status up to date during temporary Windows file locks and report startup failures that previously left jobs marked as running.

## 1.2.0 (2026-07-22)

### Added

- Preview multiple exact frames, create contact sheets, check hardware encoders, and view render history and estimated completion time.
- Work with SDR, HLG, and PQ projects. Apply constant speed changes or forward speed ramps, and use roll, slip, slide, and same-track ripple trims.
- Inspect source color metadata and find missing media by Shotcut hash or filename. Review candidate matches in a contact sheet.

### Fixed

- Apply configured folder and network restrictions when editing media and opening embedded project resources.
- Use Shotcut 26.6's native 8-bit processing mode and update media hashes after relinking.
- Limit project and render-log sizes and release completed render processes even when the client stops polling.

## 1.1.0 (2026-07-22)

### Added

- Check the local Shotcut and MLT installation, RNNoise availability, and access settings with `shotcut_doctor`.
- Preview and validate proposed edits without saving them with `plan_project_edit`.
- Monitor or cancel background renders after restarting the client. Completed exports are moved to their destination automatically.
- Cancel in-flight requests and use clients that send batched requests.
- Restrict access to selected folders, require absolute paths, and control access to network resources and custom export properties.

### Fixed

- Preserve changes saved by another program during project validation.
- Keep backups separate for similarly named projects and restore only recognized backups for the selected project.
- Prevent previews from overwriting the project. Preserve existing output when a preview or render fails or another program changes the destination.
- Restrict custom export properties to a single output file by default. Embedded network resources require administrator opt-in.
- Apply a clip-local filter without changing other clips that share its source.
- Clean up unused generated project elements and reject ambiguous timelines or duplicate identifiers.
- Report project filters, MLT links, and color resources accurately. Refresh runtime checks when the MLT environment changes.

## 1.0.1 (2026-07-21)

### Added

- Install Shotcut MCP as a portable MCP Bundle in compatible clients.

### Changed

- Use English for tool responses and documentation.

### Fixed

- Keep clips visible on the timeline when reopening an edited project in Shotcut.

## 1.0.0 (2026-07-13)

### Added

- Create and edit multitrack Shotcut projects with 16 tools and 24 timeline operations. Edits are validated with MLT and backed up before saving.
- Preview frames and monitor renders with Shotcut 26.2.26 and MLT 7.37.0.
