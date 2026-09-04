# Installation

[Shotcut MCP](../README.md) · [Workflow and examples](workflows.md) · [Technical reference](reference.md)

Install a local Shotcut MCP server and connect it to your preferred MCP client.

> **MCPB package:** compatible clients can install the
> [latest packaged release](https://github.com/matrodrigs/shotcut-mcp/releases/latest).

## Requirements

- Python 3.10 or newer
- Shotcut 26.6.25, with MLT 7.40.0 serialization in the compatible 7.40.x family
- Codex CLI, Claude Code, or another MCP client that supports local stdio servers

Additional compatibility and runtime behavior, including progress, MLT startup, and RNNoise
checks, are documented in the [behavioral specification](spec.md).

## Choose your client

| [**Codex**](#codex) | [**Claude Code**](#claude-code) | [**Other MCP clients**](#other-mcp-clients) |
| --- | --- | --- |
| Native CLI registration | Plugin marketplace | Standard MCP `stdio` |

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

> Check whether Shotcut MCP is ready and report the detected Shotcut, Melt, FFmpeg, and FFprobe
> versions.

A healthy setup reports the discovered paths, versions, repository state, RNNoise availability,
and active path policy. If anything is missing, the response should explain what needs attention.

Continue with the [editing workflow and example prompts](workflows.md). For custom executable
paths or access policies, see [configuration](reference.md#configuration).
