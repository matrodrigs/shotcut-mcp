"""Dependency-free MCP stdio protocol server."""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .errors import ToolError
from .tools import HANDLERS, TOOLS


SERVER_NAME = "shotcut-mcp"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}


def _tool_result(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        ],
        "structuredContent": payload,
        "isError": is_error,
    }


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    if "id" not in message:
        return None
    request_id = message.get("id")
    if method == "initialize":
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        requested = params.get("protocolVersion")
        protocol = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "Use inspect_project antes de editar; passe revision como expected_revision. "
                    "Consulte shotcut_capabilities para o catálogo de operações. Agrupe alterações "
                    "em uma única chamada edit_project e não use force sem autorização explícita."
                ),
            },
        }
    if method in {"ping", "logging/setLevel"}:
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        call_params = message.get("params")
        if not isinstance(call_params, dict):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Parâmetros inválidos."},
            }
        name = call_params.get("name")
        handler = HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": f"Ferramenta desconhecida: {name}",
                },
            }
        arguments = call_params.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            result = _tool_result(handler(arguments))
        except ToolError as exc:
            result = _tool_result(
                {"error": str(exc), "error_type": type(exc).__name__}, True
            )
        except Exception as exc:  # Keep the long-running stdio server alive.
            print(f"Unexpected error in {name}: {exc!r}", file=sys.stderr, flush=True)
            result = _tool_result(
                {"error": f"Falha interna inesperada: {type(exc).__name__}: {exc}"},
                True,
            )
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Método não encontrado: {method}"},
    }


def write_message(message: dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("A mensagem não é um objeto JSON.")
            response = handle_request(message)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"JSON inválido: {exc}"},
            }
        if response is not None:
            write_message(response)
    return 0
