# Installation

[Shotcut MCP](../README.md) · [Workflow and examples](workflows.md) · [Technical reference](reference.md)

Install a local Shotcut MCP server and connect it to your preferred MCP client.

> **MCPB package:** compatible clients can install the [latest packaged release](https://github.com/matrodrigs/shotcut-mcp/releases/latest).

## Requirements

- Python 3.10 or newer
- Shotcut, preferably 26.8.1 with bundled MLT 7.41.0
- Codex CLI, Claude Code, or another MCP client that supports local stdio servers

The project serialization baseline and tested runtime pairs evolve independently:

| Coverage | Versions and scope |
| --- | --- |
| New project baseline | Shotcut 26.8.1 / MLT 7.41.0 (7.41.x family); existing projects retain their metadata |
| Ordinary tests | Windows, macOS, and Linux with Python 3.10 and 3.14 |
| Real rendering integration | Windows / Python 3.10: Shotcut 26.6.25 with MLT 7.40.0, and Shotcut 26.8.1 with MLT 7.41.0, including the extracted MCPB over stdio |
| Client GUI installation | Not exercised by the automated rendering suite |

The [CI workflow](../.github/workflows/shotcut-integration.yml) exercises these versions; it does not certify every filter, encoder, GPU, or client installation.

After an upgrade, run `shotcut_doctor`: an `untested` result is a version warning, so continue normal project validation and previews. For `failed`, follow the recovery guidance in `issues`. See the [diagnostic contract](spec.md#compatibility-boundary) for field details.

## Choose your client

| Client | Setup |
| --- | --- |
| [Claude Desktop / MCPB](#claude-desktop-and-mcpb) | Packaged extension |
| [Codex](#codex) | Native CLI registration |
| [Claude Code](#claude-code) | Plugin marketplace |
| [Other MCP clients](#other-mcp-clients) | Standard MCP `stdio` |

## Claude Desktop and MCPB

Install Python and Shotcut first. The MCPB contains this server; it does not bundle Python or Shotcut. On Windows, `python --version` must find a supported Python; on macOS, use `python3 --version`. If Windows opens the Microsoft Store, follow [Python discovery](#python-discovery-on-windows) before installing. No repository clone or `pip install` is needed for this route.

1. Download `shotcut-mcp-X.Y.Z.mcpb` from the [latest release](https://github.com/matrodrigs/shotcut-mcp/releases/latest).
2. In Claude Desktop, open **Settings → Extensions → Advanced settings → Install Extension…** and select the package. Follow the installation prompts.
3. Enable the extension and start a new conversation, then run the readiness check below.

These controls follow the [official Claude Desktop extension guide](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop). Organizations can restrict custom extensions; use the administrator's deployment route when the installation control is unavailable. Other MCPB clients provide their own package installer.

To update a manually installed package, download the new MCPB and install it through the same extension settings. Check the installed version in that panel and reconnect before running the readiness check. Updating a source checkout does not update an installed MCPB.

## Codex

Use a [source checkout](#source-checkout), then register the server by its absolute path.

**Windows PowerShell**

```powershell
$pythonExe = py -3 -c "import sys; print(sys.executable)"
& $pythonExe "C:\path\to\shotcut-mcp\scripts\shotcut_mcp_server.py" --check
codex mcp add shotcut -- $pythonExe "C:\path\to\shotcut-mcp\scripts\shotcut_mcp_server.py"
```

**macOS or Linux**

```bash
python3 /absolute/path/to/shotcut-mcp/scripts/shotcut_mcp_server.py --check
codex mcp add shotcut -- python3 /absolute/path/to/shotcut-mcp/scripts/shotcut_mcp_server.py
```

Use a Python 3.10+ executable. If `py` is unavailable or selects an older installation, use the interpreter's absolute path as described under [Python discovery](#python-discovery-on-windows).

## Claude Code

Run these commands inside Claude Code. No repository clone is required.

```text
/plugin marketplace add matrodrigs/shotcut-mcp
/plugin install shotcut-mcp@matrodrigs
```

Run `/reload-plugins` to activate the plugin without restarting Claude Code.

<details>
<summary>Manual stdio registration</summary>

Use a [source checkout](#source-checkout), then register the server by its absolute path.

**Windows PowerShell**

```powershell
$pythonExe = py -3 -c "import sys; print(sys.executable)"
claude mcp add --transport stdio --scope user shotcut -- $pythonExe "C:\path\to\shotcut-mcp\scripts\shotcut_mcp_server.py"
```

**macOS or Linux**

```bash
claude mcp add --transport stdio --scope user shotcut -- python3 /absolute/path/to/shotcut-mcp/scripts/shotcut_mcp_server.py
```

From a source checkout, Claude Code can also use the checked-in `.mcp.json` as project-scoped configuration when it is started from the repository root. Review and approve the server when prompted.

</details>

## Other MCP clients

Use a [source checkout](#source-checkout) and configure a local `stdio` server:

| Setting | Value |
| --- | --- |
| Name | `shotcut` |
| Command | Absolute path to your Python 3.10+ interpreter; `python3` also works when discoverable on macOS or Linux |
| Argument | Absolute path to `scripts/shotcut_mcp_server.py` |

Restart the MCP client or open a new task after registration.

## Source checkout

For direct stdio registration, clone the repository; no `pip install` is required:

```bash
git clone https://github.com/matrodrigs/shotcut-mcp.git
cd shotcut-mcp
```

Update it with `git pull --ff-only`, then reconnect the MCP client. This updates only clients registered against that checkout; installed MCPB packages have their own update route.

## Verify the installation

Before connecting a source checkout to an MCP client, run the same entry point with `--check`:

```powershell
py -3 scripts/shotcut_mcp_server.py --check
```

On macOS or Linux, use `python3 scripts/shotcut_mcp_server.py --check`. The command prints JSON with the actual Python executable and version, detected media tools, runtime diagnostics, and active path policy. It exits without starting MCP: exit code `0` means `runtime_ready` is true; `1` means a runtime check failed. Untested versions with passing checks still return `0`. Use the reported Python executable when registering the server. Do not add `--check` to the client's MCP launch arguments.

Ask your MCP client:

> Run shotcut_status and shotcut_doctor. Report tool paths, versions, failed runtime checks, and active path policy.

`shotcut_status` reports executable paths and versions. `shotcut_doctor` diagnoses the Shotcut/MLT pair, repository startup, RNNoise, and optional FFmpeg analyzers.

## Python discovery on Windows

The `python` command can point to a Microsoft Store alias even when Python is installed. Check the interpreter independently in PowerShell:

```powershell
Get-Command python, py -ErrorAction SilentlyContinue
py -3 -c "import sys; print(sys.version); print(sys.executable)"
```

If `py` finds Python 3.10+, use the printed executable path in manual MCP registration. You can also run the preconnection check directly with that path:

```powershell
& "C:\path\to\python.exe" "C:\path\to\shotcut-mcp\scripts\shotcut_mcp_server.py" --check
```

If neither command works, locate or install Python 3.10+ for the same Windows user who runs the MCP client. The packaged MCPB and default Claude Code adapter launch `python` on Windows, so those routes also require `python --version` to work: correct `PATH` or the Windows **App execution aliases**, then restart the MCP client. An unavailable `python` alias alone does not mean that Python is absent.

## Troubleshooting and recovery

| Symptom | Next step |
| --- | --- |
| Extension cannot start | Check [Python discovery](#python-discovery-on-windows) and the extension logs in Claude Desktop's Extensions settings; confirm that the latest MCPB is installed |
| Melt, FFmpeg, or FFprobe missing | Run `shotcut_doctor`; configure the corresponding executable path from the [configuration reference](reference.md#configuration) |
| Background render fails at startup | Inspect `render_status` for its structured error and bounded `log_tail`; these are worker diagnostics, separate from client startup logs |
| Render capacity reached | Use `list_render_jobs` to find active jobs; wait for completion or cancel an unwanted job before retrying |
| Project revision conflict | Run `inspect_project` again and reconsider the edit against the current revision |
| Missing media | Run `diagnose_missing_media`, review candidates, then explicitly relink the intended source |
| Speed map has unknown source bounds | Recover the original clip from a backup or reinsert the source before replacing the map; see [limitations](reference.md#limitations) |

Continue with the [editing workflow and example prompts](workflows.md). For custom executable paths or access policies, see [configuration](reference.md#configuration).
