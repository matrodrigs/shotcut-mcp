# Installation

[Shotcut MCP](../README.md) · [Workflow and examples](workflows.md) · [Technical reference](reference.md)

Install a local Shotcut MCP server and connect it to your preferred MCP client.

> **MCPB package:** compatible clients can install the
> [latest packaged release](https://github.com/matrodrigs/shotcut-mcp/releases/latest).

## Requirements

- Python 3.10 or newer
- Shotcut 26.6.25, with MLT 7.40.0 serialization in the compatible 7.40.x family
- Codex CLI, Claude Code, or another MCP client that supports local stdio servers

The declared compatibility target and the automated test matrix serve different purposes:

| Coverage | Versions and scope |
| --- | --- |
| Declared compatibility target | Shotcut 26.6.25 / MLT 7.40.x; individual optional services still require discovery |
| Ordinary tests | Windows, macOS, and Linux with Python 3.10 and 3.14 |
| Real rendering integration | Windows with Python 3.10 and portable Shotcut 26.6.25 / 26.8.1, including the extracted MCPB over stdio |
| Client GUI installation | Not exercised by the automated rendering suite |

The [CI workflows](../.github/workflows/ci.yml) define the current matrix. Additional integration
coverage does not certify every filter, encoder, GPU, or client installation. For runtime behavior
and optional-service checks, see the [behavioral specification](spec.md).

## Choose your client

| Client | Setup |
| --- | --- |
| [Claude Desktop / MCPB](#claude-desktop-and-mcpb) | Packaged extension |
| [Codex](#codex) | Native CLI registration |
| [Claude Code](#claude-code) | Plugin marketplace |
| [Other MCP clients](#other-mcp-clients) | Standard MCP `stdio` |

## Claude Desktop and MCPB

Install Python and Shotcut first. The MCPB contains this server; it does not bundle Python or
Shotcut. On Windows, `python --version` must find a supported Python; on macOS, use
`python3 --version`. No repository clone or `pip install` is needed for this route.

1. Download `shotcut-mcp-X.Y.Z.mcpb` from the [latest release](https://github.com/matrodrigs/shotcut-mcp/releases/latest).
2. In Claude Desktop, open **Settings → Extensions → Advanced settings → Install Extension…**
   and select the package. Follow the installation prompts.
3. Enable the extension and start a new conversation, then run the readiness check below.

These controls follow the [official Claude Desktop extension guide](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop).
Organizations can restrict custom extensions; use the administrator's deployment route when
the installation control is unavailable. Other MCPB clients provide their own package installer.

To update a manually installed package, download the new MCPB and install it through the same
extension settings. Check the installed version in that panel and reconnect before running the
readiness check. Updating a source checkout does not update an installed MCPB.

## Codex

Clone the repository. No `pip install` is required.

```bash
git clone https://github.com/matrodrigs/shotcut-mcp.git
cd shotcut-mcp
```

Register the server using an absolute path to the checked-out script.

**Windows PowerShell**

```powershell
codex mcp add shotcut -- python "C:\path\to\shotcut-mcp\scripts\shotcut_mcp_server.py"
```

**macOS or Linux**

```bash
codex mcp add shotcut -- python3 /absolute/path/to/shotcut-mcp/scripts/shotcut_mcp_server.py
```

## Claude Code

Run these commands inside Claude Code. No repository clone is required.

```text
/plugin marketplace add matrodrigs/shotcut-mcp
/plugin install shotcut-mcp@matrodrigs
```

Run `/reload-plugins` to activate the plugin without restarting Claude Code.

<details>
<summary>Manual stdio registration</summary>

Clone the repository. No `pip install` is required.

```bash
git clone https://github.com/matrodrigs/shotcut-mcp.git
cd shotcut-mcp
```

Then register the server directly using an absolute path to the checked-out script.

**Windows PowerShell**

```powershell
claude mcp add --transport stdio --scope user shotcut -- python "C:\path\to\shotcut-mcp\scripts\shotcut_mcp_server.py"
```

**macOS or Linux**

```bash
claude mcp add --transport stdio --scope user shotcut -- python3 /absolute/path/to/shotcut-mcp/scripts/shotcut_mcp_server.py
```

From a source checkout, Claude Code can also use the checked-in `.mcp.json` as project-scoped
configuration when it is started from the repository root. Review and approve the server when
prompted.

</details>

## Other MCP clients

Clone the repository. No `pip install` is required.

```bash
git clone https://github.com/matrodrigs/shotcut-mcp.git
cd shotcut-mcp
```

Configure a local `stdio` server with these values:

| Setting | Value |
| --- | --- |
| Name | `shotcut` |
| Command | `python` on Windows; `python3` on macOS or Linux |
| Argument | Absolute path to `scripts/shotcut_mcp_server.py` |

The `.codex-plugin` and `.claude-plugin` manifests are thin client adapters. The Claude marketplace
entry only adds discovery and installation. Every route starts the same dependency-free Python
server; the tools, schemas, and project-safety behavior do not fork by client.

Restart the MCP client or open a new task after registration.

## Verify the installation

Ask your MCP client:

> Run shotcut_doctor for the full readiness check. Report detected versions, failed checks,
> RNNoise availability, and active path policy.

A healthy setup reports the discovered paths, versions, repository state, RNNoise availability,
and active path policy. If anything is missing, the response should explain what needs attention.
`shotcut_status` provides quick executable/version discovery; `shotcut_doctor` runs the fuller
compatibility and optional-service checks.

## Troubleshooting and recovery

| Symptom | Next step |
| --- | --- |
| Extension cannot start | Check Python discovery and the extension logs in Claude Desktop's Extensions settings; confirm that the latest MCPB is installed |
| Melt, FFmpeg, or FFprobe missing | Run `shotcut_doctor`; configure the corresponding executable path from the [configuration reference](reference.md#configuration) |
| Background render fails at startup | Inspect `render_status` for its structured error and bounded `log_tail`; these are worker diagnostics, separate from client startup logs |
| Project revision conflict | Run `inspect_project` again and reconsider the edit against the current revision |
| Missing media | Run `diagnose_missing_media`, review candidates, then explicitly relink the intended source |
| Speed map has unknown source bounds | Recover the original clip from a backup or reinsert the source before replacing the map; see [limitations](reference.md#limitations) |

Continue with the [editing workflow and example prompts](workflows.md). For custom executable
paths or access policies, see [configuration](reference.md#configuration).
