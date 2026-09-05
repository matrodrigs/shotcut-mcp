# Shotcut MCP

<p><samp>Local video editing through the Model Context Protocol.</samp></p>

Create, edit, validate, preview, and render saved [Shotcut](https://www.shotcut.org/) projects
through your AI assistant. The result stays an editable `.mlt` timeline, with transactional
writes that protect the project as you work.

[Demonstration](#demonstration) · [Quick start](#quick-start) · [Documentation](#documentation) · [Website](https://matrodrigs.github.io/shotcut-mcp/)

## Demonstration

A short H.264 export created from a native Shotcut timeline edited through Shotcut MCP.

https://github.com/user-attachments/assets/c70f064f-17e7-403d-9bcf-689a9c616cdf

## Quick start

You need **Python 3.10+**, **Shotcut 26.6.25** with MLT 7.40.0 serialization in the compatible
7.40.x family, and an MCP client that supports local `stdio` servers. Shotcut provides Melt,
FFmpeg, FFprobe, codecs, and filters; the server uses only Python's standard library.
The bundled launchers load the server and background renderer from the extension itself;
you do not need to install the Python package or configure `PYTHONPATH`.

### 1. Connect your client

| Client | Installation |
| --- | --- |
| Claude Desktop / MCPB-compatible client | Follow the [MCPB installation and update guide](docs/installation.md#claude-desktop-and-mcpb). |
| Codex | Clone the repository and [register the local server](docs/installation.md#codex). |
| Claude Code | Run `/plugin marketplace add matrodrigs/shotcut-mcp`, then `/plugin install shotcut-mcp@matrodrigs` and `/reload-plugins`. [Details and manual setup](docs/installation.md#claude-code). |
| Other MCP client | Configure a [local stdio server](docs/installation.md#other-mcp-clients). |

The [installation guide](docs/installation.md) includes commands for Windows, macOS, and Linux.
No `pip install` is required to run the server.

### 2. Check readiness

Ask your assistant:

> Run the full Shotcut MCP readiness check with shotcut_doctor and report the detected Shotcut,
> Melt, FFmpeg, and FFprobe versions and any failed checks.

A healthy setup reports discovered paths, versions, repository state, RNNoise availability,
and the active path policy. Resolve anything it flags before editing.

### 3. Make your first edit

Provide the source folder and the destination for the saved project, then describe your edit:

```text
Create a 1920×1080, 30 fps Shotcut project from every video in this folder.
Put narration on A1, add 12-frame crossfades, and save it as documentary.mlt.
Generate a contact sheet so I can review the edit before exporting.
```

You can request adjustments in ordinary language. See the [editing workflow and example
prompts](docs/workflows.md) for subtitles, quality analysis, effects, and delivery.

## Features

- **Edit the timeline:** work with tracks, clips, transitions, speed changes, titles, and subtitles;
  apply up to 500 operations in one transaction.
- **Review the result:** inspect source quality and color, preview proposed changes, and generate
  frames or contact sheets before exporting.
- **Render and deliver:** export a full project, frame range, or named marker; receive the media
  and the exact editable project used for that render.
- **Keep work local:** use effects from your installed MLT build, with revision checks,
  validation, isolated backups, and atomic file replacement.

See the [full capability reference](docs/reference.md#features) for supported edit operations.

## Transactional safety

Edits are checked against the inspected revision, validated with Melt, and backed up before
atomically replacing the saved project. Unknown XML is preserved; ambiguous edits are rejected.
Preview and render outputs also use temporary files and atomic promotion.

Each render uses an immutable project snapshot, so later timeline edits cannot change a running
export. If export was not already requested, the client confirms the output, preset, range, and
overwrite behavior first. An explicit export request does not need a second approval.

Read the [transaction and recovery details](docs/reference.md#transactional-safety) and
[rendering behavior](docs/reference.md#rendering).

## Limitations

- The server edits **saved project files**; it cannot see unsaved changes in Shotcut. Let an MCP
  edit finish before saving the same project in the GUI, then inspect it again.
- Available codecs, filters, fonts, and quality analyzers depend on your local installation.
- Some ambiguous transitions, speed maps, and cross-track edits are rejected. Changing FPS
  preserves recognized frame numbers rather than automatically retiming the edit.
- Replacing a speed map preserves its source interval after trims and splits. Older maps without
  recorded source bounds must be rebuilt from the original clip before replacement.
  Reverse maps landing on source frame zero are outside the verified MLT interface;
  use constant speed reversal or a source range above zero. [Details](docs/reference.md#limitations).
- Network resources and unsafe consumer properties are denied by default. Administrators can
  restrict filesystem access through the [runtime configuration](docs/reference.md#configuration).

See [all limitations](docs/reference.md#limitations) for edge cases and recovery behavior.

## Documentation

| Guide | What you will find |
| --- | --- |
| [Installation](docs/installation.md) | Client setup, requirements, and readiness checks |
| [Workflow and examples](docs/workflows.md) | From an edit request to visual review and delivery |
| [Technical reference](docs/reference.md) | Capabilities, tools, presets, configuration, and safety |
| [Behavioral specification](docs/spec.md) | Detailed contracts and compatibility boundaries |
| [Architecture](docs/architecture.md) | Module responsibilities and dependency direction |

### MCP tools

The [MCP tool catalog](docs/reference.md#mcp-tools) lists every tool with a concise purpose.
Clients receive full schemas and operation guidance at runtime; most users can work entirely
through natural-language prompts.

## Development

Install the pinned development tools and run the shared quality gate:

```bash
python -m pip install -r requirements-dev.txt
python -B scripts/check_ci.py all
```

Enable it before each push with `git config core.hooksPath .githooks`. Real Shotcut integration
is opt-in locally through `SHOTCUT_MCP_INTEGRATION=1`. The GitHub Actions CI gate requires real
Windows integration in addition to the ordinary cross-platform suite and static checks.

See [Contributing](CONTRIBUTING.md) for development and release procedures,
[Changelog](CHANGELOG.md) for release changes, [Issues](https://github.com/matrodrigs/shotcut-mcp/issues)
for bugs and feature requests, and [Security](SECURITY.md) for private vulnerability reporting.

## License

Released under the [MIT License](LICENSE). This is an independent community project, not
affiliated with or endorsed by Shotcut or the MLT project. Shotcut is a trademark of its respective
owner; MLT is an independent open-source multimedia framework. This repository contains no
Shotcut or MLT source code.

<sub>Created by <a href="https://github.com/matrodrigs">Mateus Rodrigues</a>.</sub>
