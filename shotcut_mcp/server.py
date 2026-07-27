"""Dependency-free MCP stdio protocol server."""

from __future__ import annotations

import base64
import copy
import json
import os
import sys
import threading
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__
from .errors import ConflictError, RequestCancelled, ToolError
from .protocol import request_cancellation, request_progress, schema_errors
from .tools import HANDLERS, TOOLS, validate_tool_arguments

SERVER_NAME = "shotcut-mcp"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
STRUCTURED_CONTENT_PROTOCOLS = {"2025-06-18", "2025-11-25"}
MAX_ERROR_DETAIL_ITEMS = 32
MAX_ERROR_DETAIL_STRING = 2000
MAX_ERROR_DETAIL_DEPTH = 4
SERVER_INSTRUCTIONS = (
    "Saved state: Use the user-supplied project path; ask if missing. Shotcut MCP sees "
    "only the project saved on disk; if Shotcut is open, ask the user to save and avoid "
    "concurrent saves.\n"
    "Normal edit: Call inspect_project first and pass expected_revision. Use "
    "shotcut_capabilities for unfamiliar operations. Prefer item_ref; use as/@alias for "
    "items created in the same batch. Use animate_clip for pan, zoom, rotation, opacity, "
    "and volume. Batch related edits. Never use force or overwrite without explicit "
    "authorization. On a revision conflict, re-inspect and reconsider; never retry with "
    "force automatically.\n"
    "Readiness: Use validate_project when readiness is unknown or media/service "
    "dependencies changed before preview or render. A successful edit_project already "
    "validates its candidate with Melt, so do not repeat validate_project routinely. "
    "valid=True means local Melt processed the first project frame; ready=True also "
    "requires local resources and required MLT services. Surface failed or unavailable "
    "checks. For missing media, use diagnose_missing_media and let the user choose before "
    "relinking; for runtime gaps, use shotcut_doctor or list_mlt_services.\n"
    "Planning and review: Use plan_project_edit for uncertain edits or user review. To "
    "show the current edit, call render_contact_sheet and surface its image when "
    "supported; use render_preview for one exact moment.\n"
    "Media diagnosis: Use diagnose_color_workflow for washed-out color or HDR questions. "
    "Use analyze_media_quality before proposing cleanup for silence, black frames, "
    "freezes, interlacing, or loudness.\n"
    "Export: Choose exactly one start_render mode: the full project, both inclusive "
    "frames, or one range marker; monitor its job_id with render_status. Use "
    "export_marker_chapters for Shotcut-compatible chapter text and list_render_jobs "
    "when the job_id is unknown.\n"
    "Recovery: For a tool result with isError=true, follow error_code, "
    "recommended_action, recommended_tool, and details instead of parsing the English "
    "message. List backups before restoring and confirm the selected backup."
)


@dataclass
class ProtocolSession:
    protocol_version: str = LATEST_PROTOCOL_VERSION
    initialized: bool = False
    enforce_lifecycle: bool = False


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
        if protocol_version not in STRUCTURED_CONTENT_PROTOCOLS:
            tool.pop("outputSchema", None)
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
    payload: dict[str, Any],
    protocol_version: str,
    is_error: bool = False,
    tool_name: str | None = None,
) -> dict[str, Any]:
    structured = protocol_version in STRUCTURED_CONTENT_PROTOCOLS
    if structured:
        if is_error:
            message = str(payload.get("error") or "Tool execution failed.")
            code = payload.get("error_code")
            text = f"{message} Error code: {code}." if code else message
        else:
            label = tool_name or "Tool"
            text = (
                f"{label} completed. The complete result is available in "
                "structuredContent."
            )
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    image = _inline_image_content(tool_name, payload) if not is_error else None
    if image is not None:
        content.append(image)
    result = {
        "content": content,
        "isError": is_error,
    }
    if structured:
        result["structuredContent"] = payload
    return result


def _inline_image_limit() -> int:
    try:
        configured = int(
            os.environ.get("SHOTCUT_MCP_MAX_INLINE_IMAGE_BYTES", "1048576")
        )
    except ValueError:
        configured = 1_048_576
    message_budget = max(0, (_message_size_limit() - 65_536) * 3 // 4)
    return max(0, min(4_194_304, configured, message_budget))


def _inline_image_content(
    tool_name: str | None, payload: dict[str, Any]
) -> dict[str, Any] | None:
    if tool_name not in {"render_preview", "render_contact_sheet"}:
        return None
    value = payload.get("path")
    if (
        not payload.get("created")
        or payload.get("managed_output") is not True
        or not isinstance(value, str)
    ):
        return None
    path = Path(value)
    mime_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(path.suffix.lower())
    limit = _inline_image_limit()
    try:
        size = path.stat().st_size
        if mime_type is None or limit <= 0 or size <= 0 or size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > limit:
        return None
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime_type,
        "annotations": {"audience": ["user"], "priority": 1.0},
    }


def _bounded_error_detail(value: Any, depth: int = 0) -> Any:
    """Keep diagnostic context useful without letting errors bypass message bounds."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[-MAX_ERROR_DETAIL_STRING:]
    if depth >= MAX_ERROR_DETAIL_DEPTH:
        return str(value)[-MAX_ERROR_DETAIL_STRING:]
    if isinstance(value, dict):
        return {
            str(key)[:128]: _bounded_error_detail(item, depth + 1)
            for key, item in list(value.items())[:MAX_ERROR_DETAIL_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_error_detail(item, depth + 1)
            for item in value[:MAX_ERROR_DETAIL_ITEMS]
        ]
    return str(value)[-MAX_ERROR_DETAIL_STRING:]


def _tool_error_payload(exc: ToolError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": str(exc)[-4000:],
        "error_type": type(exc).__name__,
        "error_code": exc.code,
        "recoverable": exc.recoverable,
        "recommended_action": exc.recommended_action,
        "recommended_tool": exc.recommended_tool,
        "details": _bounded_error_detail(exc.details),
    }
    if isinstance(exc, ConflictError):
        if exc.expected_revision is not None:
            payload["expected_revision"] = exc.expected_revision
        if exc.current_revision is not None:
            payload["current_revision"] = exc.current_revision
    return payload


def _handle_initialize(
    message: dict[str, Any],
    session: ProtocolSession,
    request_id: Any,
) -> dict[str, Any]:
    if session.enforce_lifecycle and session.initialized:
        return _error(request_id, -32600, "Server is already initialized.")
    raw_params = message.get("params")
    if not isinstance(raw_params, dict):
        return _error(request_id, -32602, "Invalid initialize parameters.")
    requested = raw_params.get("protocolVersion")
    if not isinstance(requested, str):
        return _error(request_id, -32602, "protocolVersion must be a string.")
    protocol = (
        requested
        if requested in SUPPORTED_PROTOCOL_VERSIONS
        else LATEST_PROTOCOL_VERSION
    )
    session.protocol_version = protocol
    session.initialized = True
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": SERVER_INSTRUCTIONS,
        },
    }


def _handle_tool_call(
    message: dict[str, Any],
    session: ProtocolSession,
    request_id: Any,
    progress_callback: Callable[[Any, float, float | None, str | None], None] | None,
) -> dict[str, Any]:
    call_params = message.get("params")
    if not isinstance(call_params, dict):
        return _error(request_id, -32602, "Invalid parameters.")
    name = call_params.get("name")
    handler = HANDLERS.get(name) if isinstance(name, str) else None
    if handler is None:
        return _error(request_id, -32602, f"Unknown tool: {name}")
    assert isinstance(name, str)
    arguments = call_params.get("arguments", {})
    if not isinstance(arguments, dict):
        return _error(request_id, -32602, "Tool arguments must be an object.")
    raw_meta = call_params.get("_meta", {})
    if not isinstance(raw_meta, dict):
        return _error(request_id, -32602, "Tool _meta must be an object.")
    progress_token = raw_meta.get("progressToken")
    if "progressToken" in raw_meta and (
        isinstance(progress_token, bool) or not isinstance(progress_token, (str, int))
    ):
        return _error(
            request_id,
            -32602,
            "_meta.progressToken must be a string or integer.",
        )
    tool = next(item for item in TOOLS if item["name"] == name)
    validation_errors = schema_errors(arguments, tool["inputSchema"])
    validation_errors.extend(validate_tool_arguments(name, arguments))
    if validation_errors:
        return _error(
            request_id,
            -32602,
            "Tool arguments do not match the published input contracts.",
            {
                "validationErrors": validation_errors,
                "error_code": "invalid_arguments",
                "recoverable": True,
                "recommended_action": "correct_arguments_and_retry",
                "recommended_tool": name,
                "details": {
                    "validation_errors": validation_errors[:MAX_ERROR_DETAIL_ITEMS],
                    "validation_error_count": len(validation_errors),
                },
            },
        )
    try:
        reporter = (
            (
                lambda progress, total, progress_message: progress_callback(
                    progress_token, progress, total, progress_message
                )
            )
            if progress_callback is not None and progress_token is not None
            else None
        )
        with request_progress(reporter):
            payload = handler(arguments)
        result = _tool_result(payload, session.protocol_version, tool_name=name)
    except RequestCancelled as exc:
        return _error(
            request_id,
            -32800,
            str(exc) or "Request cancelled.",
            _tool_error_payload(exc),
        )
    except ToolError as exc:
        result = _tool_result(
            _tool_error_payload(exc),
            session.protocol_version,
            True,
            name,
        )
    except Exception as exc:  # Keep the long-running stdio server alive.
        print(f"Unexpected error in {name}: {exc!r}", file=sys.stderr, flush=True)
        failure = ToolError(
            f"Unexpected internal failure: {type(exc).__name__}: {exc}",
            code="internal_failure",
            recoverable=False,
            recommended_action="report_issue",
            details={"exception_type": type(exc).__name__},
        )
        result = _tool_result(
            _tool_error_payload(failure),
            session.protocol_version,
            True,
        )
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_request(
    message: dict[str, Any],
    session: ProtocolSession | None = None,
    progress_callback: Callable[[Any, float, float | None, str | None], None]
    | None = None,
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
    if (
        active_session.enforce_lifecycle
        and not active_session.initialized
        and method != "initialize"
    ):
        return _error(request_id, -32002, "Server is not initialized.")
    if method == "initialize":
        return _handle_initialize(message, active_session, request_id)
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
        return _handle_tool_call(
            message,
            active_session,
            request_id,
            progress_callback,
        )
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
    maximum = _message_size_limit()
    if len(encoded) + 1 > maximum:
        if isinstance(message, dict) and "method" in message and "id" not in message:
            compact = dict(message)
            params = compact.get("params")
            if isinstance(params, dict) and "message" in params:
                compact["params"] = {
                    key: value for key, value in params.items() if key != "message"
                }
                encoded = json.dumps(
                    compact, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            if len(encoded) + 1 > maximum:
                return
        else:
            request_id = message.get("id") if isinstance(message, dict) else None
            fallback = _error(
                request_id,
                -32603,
                "MCP response exceeds the configured message-size limit.",
                {
                    "maximum_bytes": maximum,
                    "recommended_action": "narrow_request_or_increase_message_limit",
                },
            )
            encoded = json.dumps(
                fallback, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
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


def _pending_limit() -> int:
    try:
        configured = int(os.environ.get("SHOTCUT_MCP_MAX_PENDING", "32"))
    except ValueError:
        configured = 32
    return max(1, min(256, configured))


def _message_size_limit() -> int:
    try:
        configured = int(os.environ.get("SHOTCUT_MCP_MAX_MESSAGE_BYTES", "4194304"))
    except ValueError:
        configured = 4_194_304
    return max(1_024, min(16_777_216, configured))


class _StdioRuntime:
    """Own one stdio session's lifecycle, concurrency, and output serialization."""

    def __init__(self, output_stream: BinaryIO) -> None:
        self.session = ProtocolSession(enforce_lifecycle=True)
        self.output_stream = output_stream
        self.output_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending: dict[
            str | int | None,
            tuple[Future[Any], threading.Event, str | int | None],
        ] = {}
        self.active_progress_tokens: set[str | int] = set()
        self.executor = ThreadPoolExecutor(
            max_workers=_worker_count(), thread_name_prefix="shotcut-mcp"
        )
        self.pending_limit = _pending_limit()

    def write(self, message: Any) -> None:
        write_message(message, self.output_stream, self.output_lock)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _send_progress(
        self,
        progress_token: Any,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        params: dict[str, Any] = {
            "progressToken": progress_token,
            "progress": progress,
        }
        if total is not None:
            params["total"] = total
        if message and self.session.protocol_version != "2024-11-05":
            params["message"] = message
        self.write(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": params,
            }
        )

    def _complete(self, request_id: str | int | None, future: Future[Any]) -> None:
        with self.pending_lock:
            item = self.pending.pop(request_id, None)
            if item is not None and item[2] is not None:
                self.active_progress_tokens.discard(item[2])
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
            self.write(response)

    def _execute(
        self, message: dict[str, Any], cancellation: threading.Event
    ) -> dict[str, Any] | None:
        with request_cancellation(cancellation):
            return handle_request(message, self.session, self._send_progress)

    def _dispatch_batch(self, messages: list[Any]) -> None:
        if not messages or self.session.protocol_version != "2025-03-26":
            self.write(_error(None, -32600, "JSON-RPC batching is not supported."))
            return
        if len(messages) > self.pending_limit:
            self.write(
                _error(None, -32000, "JSON-RPC batch exceeds the request limit.")
            )
            return
        batch_responses = [
            handle_request(item, self.session, self._send_progress)
            if isinstance(item, dict)
            else _error(None, -32600, "Invalid Request in batch.")
            for item in messages
        ]
        visible = [item for item in batch_responses if item is not None]
        if visible:
            self.write(visible)

    def _cancel_notification(self, message: dict[str, Any]) -> bool:
        if message.get("method") != "notifications/cancelled" or "id" in message:
            return False
        params = message.get("params")
        request_id = params.get("requestId") if isinstance(params, dict) else None
        with self.pending_lock:
            item = self.pending.get(request_id)
        if item is not None:
            future, cancellation, _ = item
            cancellation.set()
            future.cancel()
        return True

    @staticmethod
    def _progress_token(message: dict[str, Any]) -> str | int | None:
        call_params = message.get("params")
        raw_meta = call_params.get("_meta") if isinstance(call_params, dict) else None
        raw_token = (
            raw_meta.get("progressToken") if isinstance(raw_meta, dict) else None
        )
        return (
            raw_token
            if not isinstance(raw_token, bool) and isinstance(raw_token, (str, int))
            else None
        )

    def _schedule_tool_call(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(
            request_id, (str, int, type(None))
        ):
            self.write(_error(None, -32600, "Invalid Request: invalid id."))
            return
        progress_token = self._progress_token(message)
        with self.pending_lock:
            duplicate = request_id in self.pending
            overloaded = len(self.pending) >= self.pending_limit
            duplicate_progress = (
                progress_token is not None
                and progress_token in self.active_progress_tokens
            )
        if duplicate:
            self.write(_error(request_id, -32600, "Duplicate in-flight request id."))
            return
        if overloaded:
            self.write(_error(request_id, -32000, "Too many in-flight requests."))
            return
        if duplicate_progress:
            self.write(
                _error(
                    request_id,
                    -32602,
                    "progressToken is already active on another request.",
                )
            )
            return

        cancellation = threading.Event()
        future = self.executor.submit(self._execute, message, cancellation)
        with self.pending_lock:
            self.pending[request_id] = (future, cancellation, progress_token)
            if progress_token is not None:
                self.active_progress_tokens.add(progress_token)
        future.add_done_callback(partial(self._complete, request_id))

    def dispatch(self, message: Any) -> None:
        if isinstance(message, list):
            self._dispatch_batch(message)
            return
        if not isinstance(message, dict):
            self.write(_error(None, -32600, "Invalid Request: expected a JSON object."))
            return
        if self._cancel_notification(message):
            return
        if message.get("method") == "tools/call" and "id" in message:
            self._schedule_tool_call(message)
            return
        response = handle_request(message, self.session, self._send_progress)
        if response is not None:
            self.write(response)


def serve(input_stream: BinaryIO, output_stream: BinaryIO) -> None:
    runtime = _StdioRuntime(output_stream)
    message_size_limit = _message_size_limit()
    try:
        while True:
            raw_line = input_stream.readline(message_size_limit + 1)
            if not raw_line:
                break
            if len(raw_line) > message_size_limit:
                while raw_line and not raw_line.endswith(b"\n"):
                    raw_line = input_stream.readline(message_size_limit + 1)
                runtime.write(
                    _error(
                        None, -32600, "MCP message exceeds the configured size limit."
                    )
                )
                continue
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                runtime.write(_error(None, -32700, f"Invalid JSON: {exc}"))
                continue
            runtime.dispatch(message)
    finally:
        runtime.close()


def main() -> int:
    serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0
