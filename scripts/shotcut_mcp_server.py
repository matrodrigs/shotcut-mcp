#!/usr/bin/env python3
"""Plugin entry point for the Shotcut MCP stdio server."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import IO

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from shotcut_mcp.errors import ToolError  # noqa: E402
from shotcut_mcp.platform import compatibility_doctor, status  # noqa: E402
from shotcut_mcp.processes import client_stdio  # noqa: E402
from shotcut_mcp.protocol import is_array, is_object  # noqa: E402
from shotcut_mcp.server import (  # noqa: E402
    LATEST_PROTOCOL_VERSION,
    SERVER_NAME,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from shotcut_mcp.server import main as serve_main  # noqa: E402


def _read_reply(stream: IO[bytes], request_id: int) -> dict[str, object]:
    replies: queue.Queue[bytes] = queue.Queue(1)

    def read() -> None:
        try:
            replies.put(stream.readline(4 * 1024 * 1024 + 1))
        except (OSError, ValueError):
            replies.put(b"")

    threading.Thread(target=read, daemon=True).start()
    try:
        line = replies.get(timeout=10)
    except queue.Empty as exc:
        raise ValueError("Server did not reply within 10 seconds.") from exc
    if not line:
        raise ValueError(
            "Server closed stdout before replying. Check the configured command, arguments and working directory."
        )
    if not line.endswith(b"\n") or len(line) > 4 * 1024 * 1024:
        raise ValueError("Server emitted an oversized or incomplete MCP message.")
    reply: object = json.loads(line)
    if (
        not is_object(reply)
        or reply.get("id") != request_id
        or reply.get("jsonrpc") != "2.0"
    ):
        raise ValueError("Server returned an unexpected JSON-RPC response.")
    result = reply.get("result")
    if not is_object(result):
        raise ValueError("Server rejected the MCP request.")
    return result


def _check_client(command: list[str], cwd: Path) -> dict[str, object]:
    stage = "launch"
    try:
        with client_stdio(command, cwd) as child:
            assert child.stdin and child.stdout

            def send(message: dict[str, object]) -> None:
                assert child.stdin
                child.stdin.write(
                    (json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode()
                )
                child.stdin.flush()

            stage = "initialize"
            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "clientInfo": {
                            "name": "shotcut-connection-check",
                            "version": "1",
                        },
                        "capabilities": {},
                    },
                }
            )
            result = _read_reply(child.stdout, 1)
            info = result.get("serverInfo")
            version = result.get("protocolVersion")
            if (
                not is_object(info)
                or info.get("name") != SERVER_NAME
                or not isinstance(version, str)
                or version not in SUPPORTED_PROTOCOL_VERSIONS
            ):
                raise ValueError(
                    "The configured command did not negotiate a supported Shotcut MCP session."
                )
            stage = "tools/list"
            send({"method": "notifications/initialized"})
            send({"id": 2, "method": "tools/list", "params": {}})
            tools = _read_reply(child.stdout, 2).get("tools")
            if not is_array(tools) or not any(
                is_object(tool) and tool.get("name") == "inspect_project"
                for tool in tools
            ):
                raise ValueError("The tool catalog does not include inspect_project.")
            return {
                "connected": True,
                "protocol_version": version,
                "tool_count": len(tools),
                "cwd": str(cwd),
            }
    except (OSError, ValueError) as exc:
        return {"connected": False, "stage": stage, "error": str(exc), "cwd": str(cwd)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="print Python and Shotcut readiness as JSON, then exit without starting MCP",
    )
    parser.add_argument(
        "--check-client",
        action="store_true",
        help="test the configured client launch command over MCP stdio",
    )
    parser.add_argument(
        "--cwd", type=Path, help="working directory used by the MCP client"
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="exact client command and arguments after --",
    )
    args = parser.parse_args(argv)
    if args.check_client:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if args.check or not command:
            parser.error(
                "--check-client requires a launch command after -- and is separate from --check"
            )
        connection = _check_client(command, (args.cwd or Path.cwd()).resolve())
        print(json.dumps(connection, indent=2))
        return 0 if connection["connected"] else 1
    if args.cwd or args.command:
        parser.error("--cwd and a launch command require --check-client")
    if not args.check:
        return serve_main()

    report: dict[str, object] = {
        "python": {"executable": sys.executable, "version": sys.version.split()[0]},
        "runtime_ready": False,
    }
    try:
        runtime = status()
        report["runtime"] = runtime
        doctor = compatibility_doctor(runtime)
        report["doctor"] = doctor
        report["runtime_ready"] = doctor["runtime_ready"]
    except ToolError as exc:
        report["error"] = str(exc)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["runtime_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
