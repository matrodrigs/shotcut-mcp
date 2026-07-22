"""Dependency-free MCP stdio protocol server."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, BinaryIO

from . import __version__
from .errors import RequestCancelled, ToolError
from .protocol import request_cancellation, schema_errors
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


def _error(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
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
    if isinstance(request_id, bool) or not isinstance(
        request_id, (str, int, type(None))
    ):
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
        list_params = message.get("params", {})
        if not isinstance(list_params, dict):
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
            result = _tool_result(handler(arguments), active_session.protocol_version)
        except RequestCancelled as exc:
            return _error(request_id, -32800, str(exc) or "Request cancelled.")
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


def write_message(
    message: Any,
    stream: BinaryIO | None = None,
    lock: threading.Lock | None = None,
) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    output = stream or sys.stdout.buffer
    if lock is None:
        output.write(encoded + b"\n")
        output.flush()
        return
    with lock:
        output.write(encoded + b"\n")
        output.flush()


def _worker_count() -> int:
    try:
        configured = int(os.environ.get("SHOTCUT_MCP_MAX_WORKERS", "4"))
    except ValueError:
        configured = 4
    return max(1, min(8, configured))


def serve(input_stream: BinaryIO, output_stream: BinaryIO) -> None:
    session = ProtocolSession()
    output_lock = threading.Lock()
    pending_lock = threading.Lock()
    pending: dict[str | int | None, tuple[Future[Any], threading.Event]] = {}
    executor = ThreadPoolExecutor(
        max_workers=_worker_count(), thread_name_prefix="shotcut-mcp"
    )

    def complete(request_id: str | int | None, future: Future[Any]) -> None:
        with pending_lock:
            pending.pop(request_id, None)
        if future.cancelled():
            response = _error(request_id, -32800, "Request cancelled.")
        else:
            try:
                response = future.result()
            except CancelledError:
                response = _error(request_id, -32800, "Request cancelled.")
            except Exception as exc:
                print(
                    f"Unexpected request worker failure: {exc!r}",
                    file=sys.stderr,
                    flush=True,
                )
                response = _error(request_id, -32603, "Internal error.")
        if response is not None:
            write_message(response, output_stream, output_lock)

    def execute(
        message: dict[str, Any], cancellation: threading.Event
    ) -> dict[str, Any] | None:
        with request_cancellation(cancellation):
            return handle_request(message, session)

    try:
        for raw_line in input_stream:
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                write_message(
                    _error(None, -32700, f"Invalid JSON: {exc}"),
                    output_stream,
                    output_lock,
                )
                continue
            if isinstance(message, list):
                if not message or session.protocol_version != "2025-03-26":
                    write_message(
                        _error(None, -32600, "JSON-RPC batching is not supported."),
                        output_stream,
                        output_lock,
                    )
                    continue
                batch_responses = [
                    handle_request(item, session)
                    if isinstance(item, dict)
                    else _error(None, -32600, "Invalid Request in batch.")
                    for item in message
                ]
                visible = [item for item in batch_responses if item is not None]
                if visible:
                    write_message(visible, output_stream, output_lock)
                continue
            if not isinstance(message, dict):
                write_message(
                    _error(None, -32600, "Invalid Request: expected a JSON object."),
                    output_stream,
                    output_lock,
                )
                continue

            if (
                message.get("method") == "notifications/cancelled"
                and "id" not in message
            ):
                params = message.get("params")
                request_id = (
                    params.get("requestId") if isinstance(params, dict) else None
                )
                with pending_lock:
                    item = pending.get(request_id)
                if item is not None:
                    future, cancellation = item
                    cancellation.set()
                    future.cancel()
                continue

            if message.get("method") == "tools/call" and "id" in message:
                request_id = message.get("id")
                if isinstance(request_id, bool) or not isinstance(
                    request_id, (str, int, type(None))
                ):
                    write_message(
                        _error(None, -32600, "Invalid Request: invalid id."),
                        output_stream,
                        output_lock,
                    )
                    continue
                with pending_lock:
                    duplicate = request_id in pending
                if duplicate:
                    write_message(
                        _error(request_id, -32600, "Duplicate in-flight request id."),
                        output_stream,
                        output_lock,
                    )
                    continue
                cancellation = threading.Event()
                future = executor.submit(execute, message, cancellation)
                with pending_lock:
                    pending[request_id] = (future, cancellation)

                def finish(
                    completed: Future[Any],
                    key: str | int | None = request_id,
                ) -> None:
                    complete(key, completed)

                future.add_done_callback(finish)
                continue

            response = handle_request(message, session)
            if response is not None:
                write_message(response, output_stream, output_lock)
    finally:
        executor.shutdown(wait=True, cancel_futures=False)


def main() -> int:
    serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0
