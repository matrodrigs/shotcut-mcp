# Shotcut MCP robust editor specification

## Goal

Provide a fast, local and reliable MCP server that can create, inspect, edit, validate,
preview and render saved Shotcut 26.6 projects without requiring a network service.

## Required behavior

- Preserve unknown MLT XML elements, attributes and properties when editing.
- Apply many edits in one parse/write transaction.
- Guard writes with a revision hash and an MCP lock file.
- Validate the edited temporary project before replacing the original.
- Create a timestamped backup before every successful replacement.
- Never overwrite a project, media file or render output without an explicit flag.
- Support video and audio tracks, gaps, clips, trim, split, move, ripple/overwrite edits,
  crossfades, generic MLT filters, keyframed properties, text/color/tone generators,
  markers, project notes, subtitle feeds and media relinking.
- Expose MLT service discovery so callers can use filters and transitions installed with
  the user's Shotcut build instead of relying on a hard-coded catalog.
- Keep project inspection and render-job management from the original MCP.
- Return stable, structured JSON from every tool and use English for public tool descriptions,
  server instructions and error messages.

## Compatibility boundary

- Target the installed Shotcut 26.6.25 and MLT 7.40.0 formats.
- Warm and retry cold MLT repository initialization before validation, preview and rendering,
  while keeping every installed service available and caching readiness by executable identity.
- Work on saved `.mlt`/MLT XML projects. Unsaved GUI state is out of scope.
- Preserve unsupported structures, but reject an edit when a target is ambiguous or when
  modifying it would require guessing about an unknown transition layout.
- Generic filters accept native MLT properties; the MCP does not promise that every third-party
  filter is available or renderable on every machine.

## Verification

- MCP protocol smoke tests.
- Unit tests through public project-editing APIs.
- Fixture tests for preservation and optimistic concurrency.
- Real ffmpeg/ffprobe/melt integration covering multitrack creation, editing, validation,
  preview and final render.
- Plugin manifest validation and a two-axis review against this specification.
