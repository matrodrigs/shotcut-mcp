"""Dependency-free MCP stdio protocol server."""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass
from typing import Any

from . import __version__
from .errors import ToolError
from .protocol import schema_errors
from .tools import HANDLERS, TOOLS


SERVER_NAME = "shotcut-mcp"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
STRUCTURED_CONTENT_PROTOCOLS = {"2025-06-18", "2025-11-25"}


@dataclass
class ProtocolSession:
    protocol_version: str = LATEST_PROTOCOL_VERSION
    initialized: bool = False


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _tools_for_version(protocol_version: str) -> list[dict[str, Any]]:
    tools = copy.deepcopy(TOOLS)
    for tool in tools:
        if protocol_version == "2024-11-05":
            tool.pop("title", None)
            tool.pop("annotations", None)
        elif protocol_version == "2025-03-26":
            title = tool.pop("title", None)
            annotations = tool.setdefault("annotations", {})
            if title and isinstance(annotations, dict):
                annotations["title"] = title
    return tools


def _tool_result(
    payload: dict[str, Any], protocol_version: str, is_error: bool = False
) -> dict[str, Any]:
    result = {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        ],
        "isError": is_error,
    }
    if protocol_version in STRUCTURED_CONTENT_PROTOCOLS:
        result["structuredContent"] = payload
    return result


def handle_request(
    message: dict[str, Any], session: ProtocolSession | None = None
) -> dict[str, Any] | None:
    active_session = session or ProtocolSession()
    request_id = message.get("id")
    if message.get("jsonrpc") != "2.0":
        return _error(request_id, -32600, "Invalid Request: jsonrpc must be '2.0'.")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        return _error(request_id, -32600, "Invalid Request: method must be a string.")
    if "id" not in message:
        return None
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int, type(None))):
        return _error(None, -32600, "Invalid Request: id must be a string or number.")
    if method == "initialize":
        raw_params = message.get("params")
        if not isinstance(raw_params, dict):
            return _error(request_id, -32602, "Invalid initialize parameters.")
        params: dict[str, Any] = raw_params
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            return _error(request_id, -32602, "protocolVersion must be a string.")
        protocol = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        active_session.protocol_version = protocol
        active_session.initialized = True
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "Use inspect_project before editing and pass its revision as expected_revision. "
                    "Consult shotcut_capabilities for the operation catalog. Group related changes "
                    "in one edit_project call and do not use force without explicit authorization."
                ),
            },
        }
    if method in {"ping", "logging/setLevel"}:
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid tools/list parameters.")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": _tools_for_version(active_session.protocol_version)},
        }
    if method == "tools/call":
        call_params = message.get("params")
        if not isinstance(call_params, dict):
            return _error(request_id, -32602, "Invalid parameters.")
        name = call_params.get("name")
        handler = HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return _error(request_id, -32602, f"Unknown tool: {name}")
        arguments = call_params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "Tool arguments must be an object.")
        tool = next(item for item in TOOLS if item["name"] == name)
        validation_errors = schema_errors(arguments, tool["inputSchema"])
        if validation_errors:
            return _error(
                request_id,
                -32602,
                "Tool arguments do not match inputSchema.",
                {"validationErrors": validation_errors},
            )
        try:
            result = _tool_result(
                handler(arguments), active_session.protocol_version
            )
        except ToolError as exc:
            result = _tool_result(
                {"error": str(exc), "error_type": type(exc).__name__},
                active_session.protocol_version,
                True,
            )
        except Exception as exc:  # Keep the long-running stdio server alive.
            print(f"Unexpected error in {name}: {exc!r}", file=sys.stderr, flush=True)
            result = _tool_result(
                {"error": f"Unexpected internal failure: {type(exc).__name__}: {exc}"},
                active_session.protocol_version,
                True,
            )
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def write_message(message: dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    session = ProtocolSession()
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("The message is not a JSON object.")
            response = handle_request(message, session)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Invalid JSON: {exc}"},
            }
        if response is not None:
            write_message(response)
    return 0
