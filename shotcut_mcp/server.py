"""Dependency-free MCP stdio protocol server."""

from __future__ import annotations

import base64
import copy
import json
import os
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import BinaryIO

from . import __version__
from .errors import ConflictError, RequestCancelled, ToolError
from .protocol import (
    is_array,
    is_object,
    request_cancellation,
    request_progress,
    schema_errors,
)
from .tools import (
    HANDLERS,
    PROJECT_COMMIT_FIELDS,
    TOOLS,
    ToolDefinition,
    validate_tool_arguments,
)

SERVER_NAME = "shotcut-mcp"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
STRUCTURED_CONTENT_PROTOCOLS = {"2025-06-18", "2025-11-25"}
RESOURCE_LINK_PROTOCOLS = {"2025-06-18", "2025-11-25"}
MAX_ERROR_DETAIL_ITEMS = 32
MAX_ERROR_DETAIL_STRING = 2000
MAX_ERROR_DETAIL_DEPTH = 4
SERVER_INSTRUCTIONS = (
    "Saved state: Use the supplied project path. Shotcut MCP sees "
    "only the project saved on disk; if there are unsaved GUI edits, ask the user to save and avoid "
    "concurrent saves.\n"
    "Normal edit: Call inspect_project first and pass expected_revision. Use "
    "shotcut_capabilities for unfamiliar operations. Prefer item_ref; use as/@alias for "
    "items created in the same batch. Use animate_clip for pan, zoom, rotation, opacity, "
    "and volume. Batch related edits. Never use force or overwrite without explicit "
    "authorization. On a revision conflict, re-inspect and reconsider; never retry with "
    "force automatically.\n"
    "Inspection pages: inspect_project defaults to 100 entries per collection page, plus owning track metadata. "
    "Use track/frame filters or detail=summary to focus, and follow pagination.next_offset "
    "with expected_revision for more. A result_truncated mutation receipt still means the "
    "change committed: inspect that revision rather than repeating the edit.\n"
    "Creating: When asked to create a project, use create_project. Choose a descriptive "
    ".mlt filename in the supplied destination folder; ask for a destination only if "
    "none is available. Use discover_media for a source folder, then probe representative "
    "files before choosing an unspecified profile.\n"
    "Creative decisions: Follow the user's brief and constraints. Make reasonable "
    "choices for pacing, cuts, transitions, framing, titles, and sound within that brief; "
    "briefly state material assumptions. Ask only when missing inputs or a decision "
    "would materially change the scope or intended result, not for every ordinary edit. "
    "Treat examples as starting points, not fixed recipes. Discover installed effects "
    "with list_mlt_services and describe_mlt_service, and use focused "
    "shotcut_capabilities schemas before constructing unfamiliar edits. Explain the "
    "visual and audio result in the user's language. Base claims about existing footage "
    "on inspected media and previews; distinguish proposed choices from verified results.\n"
    "Readiness: Use validate_project when readiness is unknown or media/service "
    "dependencies changed before preview or render. A successful edit_project already "
    "validates its candidate with Melt, so do not repeat validate_project routinely. "
    "valid=True means local Melt processed the first project frame; ready=True also "
    "requires local resources and required MLT services. Surface failed or unavailable "
    "checks. For missing media, use diagnose_missing_media; apply mappings the user "
    "already specified, otherwise ask them to choose the source before relinking. "
    "For runtime gaps, use shotcut_doctor or list_mlt_services. Doctor status=untested "
    "is a version warning: continue normal validation and previews; status=failed "
    "reports concrete runtime issues. compatible is true only for status=tested.\n"
    "Planning and review: Use plan_project_edit for uncertain edits or user review. To "
    "show the current edit, call render_contact_sheet and surface its image when "
    "supported; use render_preview for one exact moment. After inspection or a committed "
    "edit batch, give the user a concise result summary; after visual edits, show a "
    "managed contact sheet or exact preview. Use render_review_clip for up to 30 seconds "
    "of movement and sound, and show the returned playable resource or path. This managed "
    "review does not authorize final export.\n"
    "Media diagnosis: Use diagnose_color_workflow for washed-out color or HDR questions. "
    "Use analyze_media_quality before proposing cleanup for silence, black frames, "
    "freezes, interlacing, or loudness.\n"
    "Export approval: Never infer export from completed editing, validation, or visual "
    "review. Call start_render only when the user explicitly requested export in the "
    "active task, or after presenting project, output, preset, range or duration, and "
    "overwrite behavior and receiving approval. An explicit export request is already "
    "approval; do not ask twice, and never infer overwrite authorization. Pass the "
    "inspected expected_revision and choose exactly one mode: full project, both "
    "inclusive frames, or one range marker. Tell the user when the durable job starts; "
    "poll render_status until terminal and surface meaningful progress, status, or ETA "
    "changes without repeating unchanged polls or raw logs. On completion, present both "
    "returned artifacts: rendered media and the exact editable project. Use "
    "list_render_jobs when job_id is unknown and export_marker_chapters for chapters.\n"
    "Recovery: For a tool result with isError=true, follow error_code, "
    "recommended_action, recommended_tool, and details instead of parsing the English "
    "message. List backups before restoring; confirm the selected backup unless the "
    "user already identified and authorized that restoration."
)


@dataclass
class ProtocolSession:
    protocol_version: str = LATEST_PROTOCOL_VERSION
    initialized: bool = False
    enforce_lifecycle: bool = False


def _error(
    request_id: object, code: int, message: str, data: object = None
) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _tools_for_version(protocol_version: str) -> list[ToolDefinition]:
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
            if title and is_object(annotations):
                annotations["title"] = title
    return tools


def _tool_result(
    payload: Mapping[str, object],
    protocol_version: str,
    is_error: bool = False,
    tool_name: str | None = None,
    max_result_bytes: int | None = None,
) -> dict[str, object]:
    budget = (
        max_result_bytes
        if max_result_bytes is not None
        else _message_size_limit() - 256
    )
    structured = protocol_version in STRUCTURED_CONTENT_PROTOCOLS
    if structured:
        if is_error:
            message = str(payload.get("error") or "Tool execution failed.")
            code = payload.get("error_code")
            text = f"{message} Error code: {code}." if code else message
        else:
            text = _success_result_text(tool_name, payload)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    content: list[dict[str, object]] = [{"type": "text", "text": text}]
    image = _inline_image_content(tool_name, payload) if not is_error else None
    if image is not None:
        content.append(image)
    if not is_error and protocol_version in RESOURCE_LINK_PROTOCOLS:
        content.extend(_artifact_resource_links(tool_name, payload))
    result = {
        "content": content,
        "isError": is_error,
    }
    if structured:
        result["structuredContent"] = payload
    committed_field = PROJECT_COMMIT_FIELDS.get(tool_name or "")
    if (
        not is_error
        and committed_field is not None
        and payload.get(committed_field) is True
        and payload.get("result_truncated") is not True
        and _encoded_size(result, budget) > budget
    ):
        receipt = _commit_receipt(committed_field, payload["revision"])
        result = _tool_result(
            receipt, protocol_version, tool_name=tool_name, max_result_bytes=budget
        )
        for key in (
            "path",
            "previous_revision",
            "backup_path",
            "item_bindings",
            "operation_results",
            "validation",
        ):
            if key not in payload:
                continue
            candidate = {**receipt, key: payload[key]}
            formatted = _tool_result(
                candidate,
                protocol_version,
                tool_name=tool_name,
                max_result_bytes=budget,
            )
            if _encoded_size(formatted, budget) <= budget:
                receipt = candidate
                result = formatted
    return result


def _commit_receipt(committed_field: str, revision: object) -> dict[str, object]:
    return {
        committed_field: True,
        "revision": revision,
        "result_truncated": True,
        "recommended_tool": "inspect_project",
    }


def _encoded_size(value: object, maximum: int | None = None) -> int:
    size = 0
    for part in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(
        value
    ):
        size += len(part.encode("utf-8"))
        if maximum is not None and size > maximum:
            break
    return size


def _success_result_text(tool_name: str | None, payload: Mapping[str, object]) -> str:
    if payload.get("result_truncated") is True:
        return "Project change committed. Do not repeat the edit."
    if tool_name == "render_review_clip":
        return "Audiovisual review ready. Show the playable MP4 resource or path; this temporary review is replaced by the next review for this project."
    if tool_name == "shotcut_doctor":
        status = payload.get("status")
        if status == "untested":
            return (
                "Runtime checks passed, but this Shotcut/MLT pair is untested. "
                "Report the version warning and continue normal project validation and previews."
            )
        if status == "failed":
            return "Runtime checks failed. Explain the issues and recovery guidance in structuredContent."
        return "Tested Shotcut/MLT pair; runtime checks passed. Project-specific validation still applies."
    if tool_name == "edit_project":
        operations = payload.get("operation_results")
        count = len(operations) if is_array(operations) else 0
        return (
            f"Project edit committed with {count} operation result(s). Summarize the "
            "changes for the user and show a visual review when the edits are visual. "
            "Complete details are in structuredContent."
        )
    if tool_name == "start_render":
        job_id = payload.get("job_id")
        return (
            f"Durable render job {job_id} started. Tell the user it started, then poll "
            "render_status until a terminal state and surface meaningful changes."
        )
    if tool_name == "render_status":
        status = payload.get("status")
        if status == "completed" and payload.get("delivery_complete") is True:
            return (
                "Render completed. Present both attached artifacts to the user: the "
                "rendered media and the exact editable Shotcut project."
            )
        progress = payload.get("progress_percent")
        eta = payload.get("eta_seconds")
        detail = (
            f" Progress: {progress}%." if isinstance(progress, (int, float)) else ""
        )
        if isinstance(eta, (int, float)):
            detail += f" ETA: {round(eta)} seconds."
        return (
            f"Render status is {status}.{detail} Continue monitoring until terminal, "
            "but do not narrate unchanged polls."
        )
    label = tool_name or "Tool"
    return f"{label} completed. The complete result is available in structuredContent."


def _artifact_resource_links(
    tool_name: str | None, payload: Mapping[str, object]
) -> list[dict[str, object]]:
    if tool_name == "render_review_clip" and payload.get("created") is True:
        path, uri, size = (
            payload.get("path"),
            payload.get("uri"),
            payload.get("size_bytes"),
        )
        if (
            isinstance(path, str)
            and isinstance(uri, str)
            and isinstance(size, int)
            and not isinstance(size, bool)
        ):
            return [
                {
                    "type": "resource_link",
                    "uri": uri,
                    "name": Path(path).name,
                    "title": "Audiovisual review",
                    "mimeType": "video/mp4",
                    "size": size,
                    "description": "Temporary project review with movement and sound; replaced by the next managed review.",
                    "annotations": {"audience": ["user"], "priority": 1.0},
                }
            ]
        return []
    if tool_name != "render_status" or payload.get("delivery_complete") is not True:
        return []
    artifacts = payload.get("artifacts")
    if not is_array(artifacts):
        return []
    links: list[dict[str, object]] = []
    descriptions = {
        "rendered_media": "Completed rendered media",
        "editable_project": "Exact editable Shotcut project used for this render",
    }
    for artifact in artifacts:
        if not is_object(artifact):
            continue
        kind = artifact.get("kind")
        uri = artifact.get("uri")
        path = artifact.get("path")
        mime_type = artifact.get("mime_type")
        size = artifact.get("size_bytes")
        if (
            kind not in descriptions
            or not isinstance(uri, str)
            or not isinstance(path, str)
            or not isinstance(mime_type, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
        ):
            continue
        links.append(
            {
                "type": "resource_link",
                "uri": uri,
                "name": Path(path).name,
                "title": (
                    "Rendered media"
                    if kind == "rendered_media"
                    else "Editable Shotcut project"
                ),
                "description": descriptions[kind],
                "mimeType": mime_type,
                "size": size,
                "annotations": {"audience": ["user"], "priority": 1.0},
            }
        )
    return links


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
    tool_name: str | None, payload: Mapping[str, object]
) -> dict[str, object] | None:
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


def _bounded_error_detail(value: object, depth: int = 0) -> object:
    """Keep diagnostic context useful without letting errors bypass message bounds."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[-MAX_ERROR_DETAIL_STRING:]
    if depth >= MAX_ERROR_DETAIL_DEPTH:
        return str(value)[-MAX_ERROR_DETAIL_STRING:]
    if is_object(value):
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


def _tool_error_payload(exc: ToolError) -> dict[str, object]:
    payload: dict[str, object] = {
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
    message: dict[str, object],
    session: ProtocolSession,
    request_id: object,
) -> dict[str, object]:
    if session.enforce_lifecycle and session.initialized:
        return _error(request_id, -32600, "Server is already initialized.")
    raw_params = message.get("params")
    if not is_object(raw_params):
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
    message: dict[str, object],
    session: ProtocolSession,
    request_id: object,
    progress_callback: Callable[[object, float, float | None, str | None], None] | None,
    max_response_bytes: int | None,
) -> dict[str, object]:
    call_params = message.get("params")
    if not is_object(call_params):
        return _error(request_id, -32602, "Invalid parameters.")
    name = call_params.get("name")
    handler = HANDLERS.get(name) if isinstance(name, str) else None
    if handler is None:
        return _error(request_id, -32602, f"Unknown tool: {name}")
    assert isinstance(name, str)
    arguments = call_params.get("arguments", {})
    if not is_object(arguments):
        return _error(request_id, -32602, "Tool arguments must be an object.")
    envelope_bytes = (
        _encoded_size({"jsonrpc": "2.0", "id": request_id, "result": {}}) - 2 + 1
    )
    maximum = (
        _message_size_limit() if max_response_bytes is None else max_response_bytes
    )
    result_budget = maximum - envelope_bytes
    committed_field = PROJECT_COMMIT_FIELDS.get(name)
    if committed_field:
        minimum_receipt = _tool_result(
            _commit_receipt(committed_field, "0" * 64),
            session.protocol_version,
            tool_name=name,
            max_result_bytes=result_budget,
        )
        if _encoded_size(minimum_receipt) > result_budget:
            return _error(
                request_id,
                -32602,
                "Use a shorter request id so the commit receipt fits the message limit.",
            )
    raw_meta = call_params.get("_meta", {})
    if not is_object(raw_meta):
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
        result = _tool_result(
            payload,
            session.protocol_version,
            tool_name=name,
            max_result_bytes=result_budget,
        )
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
    message: dict[str, object],
    session: ProtocolSession | None = None,
    progress_callback: Callable[[object, float, float | None, str | None], None]
    | None = None,
    *,
    max_response_bytes: int | None = None,
) -> dict[str, object] | None:
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
        if not is_object(list_params):
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
            max_response_bytes,
        )
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _response_size_error(request_id: object, maximum: int) -> dict[str, object]:
    response = _error(
        request_id,
        -32603,
        "MCP response exceeds the configured message-size limit.",
        {
            "maximum_bytes": maximum,
            "recommended_action": "narrow_request_or_increase_message_limit",
        },
    )
    if _encoded_size(response, maximum) + 1 > maximum:
        response = _error(request_id, -32603, "MCP response is too large.")
    if _encoded_size(response, maximum) + 1 > maximum:
        response = _error(None, -32603, "MCP response is too large.")
    return response


def write_message(
    message: object,
    stream: BinaryIO | None = None,
    lock: threading.Lock | None = None,
) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    maximum = _message_size_limit()
    if len(encoded) + 1 > maximum:
        if is_object(message) and "method" in message and "id" not in message:
            compact = dict(message)
            params = compact.get("params")
            if is_object(params) and "message" in params:
                compact["params"] = {
                    key: value for key, value in params.items() if key != "message"
                }
                encoded = json.dumps(
                    compact, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            if len(encoded) + 1 > maximum:
                return
        else:
            request_id = message.get("id") if is_object(message) else None
            fallback = _response_size_error(request_id, maximum)
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
            tuple[Future[dict[str, object] | None], threading.Event, str | int | None],
        ] = {}
        self.active_progress_tokens: set[str | int] = set()
        self.executor = ThreadPoolExecutor(
            max_workers=_worker_count(), thread_name_prefix="shotcut-mcp"
        )
        self.pending_limit = _pending_limit()

    def write(self, message: object) -> None:
        write_message(message, self.output_stream, self.output_lock)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _send_progress(
        self,
        progress_token: object,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        params: dict[str, object] = {
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

    def _complete(
        self,
        request_id: str | int | None,
        respond: Callable[[dict[str, object] | None], None],
        future: Future[dict[str, object] | None],
    ) -> None:
        with self.pending_lock:
            item = self.pending.pop(request_id, None)
            if item is not None and item[2] is not None:
                self.active_progress_tokens.discard(item[2])
        response: dict[str, object] | None
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
        respond(response)

    def _execute(
        self,
        message: dict[str, object],
        cancellation: threading.Event,
        max_response_bytes: int | None,
    ) -> dict[str, object] | None:
        with request_cancellation(cancellation):
            return handle_request(
                message,
                self.session,
                self._send_progress,
                max_response_bytes=max_response_bytes,
            )

    def _dispatch_batch(self, messages: list[object]) -> None:
        if not messages or self.session.protocol_version != "2025-03-26":
            self.write(_error(None, -32600, "JSON-RPC batching is not supported."))
            return
        if len(messages) > self.pending_limit:
            self.write(
                _error(None, -32000, "JSON-RPC batch exceeds the request limit.")
            )
            return
        requests = [
            message
            for message in messages
            if not (
                is_object(message)
                and message.get("jsonrpc") == "2.0"
                and isinstance(message.get("method"), str)
                and message.get("method")
                and "id" not in message
            )
        ]
        response_budget = (_message_size_limit() - 2) // max(1, len(requests))
        for message in requests:
            request_id = message.get("id") if is_object(message) else None
            if isinstance(request_id, bool) or not isinstance(
                request_id, (str, int, type(None))
            ):
                request_id = None
            minimum = _response_size_error(request_id, response_budget)
            params = message.get("params") if is_object(message) else None
            name = params.get("name") if is_object(params) else None
            committed_field = (
                PROJECT_COMMIT_FIELDS.get(name) if isinstance(name, str) else None
            )
            if committed_field and isinstance(name, str):
                minimum = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": _tool_result(
                        _commit_receipt(committed_field, "0" * 64),
                        self.session.protocol_version,
                        tool_name=name,
                    ),
                }
            if (
                minimum.get("id") != request_id
                or _encoded_size(minimum, response_budget) + 1 > response_budget
            ):
                self.write(
                    _error(
                        None,
                        -32600,
                        "Batch response budget is too small. Send fewer requests or shorter ids.",
                    )
                )
                return
        # Count notifications too so fast workers cannot flush a partial batch.
        remaining = len(messages)
        responses: list[dict[str, object]] = []
        lock = threading.Lock()

        def collect(response: dict[str, object] | None) -> None:
            nonlocal remaining
            with lock:
                if response is not None:
                    if _encoded_size(response, response_budget) + 1 > response_budget:
                        response = _response_size_error(
                            response.get("id"), response_budget
                        )
                    responses.append(response)
                remaining -= 1
                completed = list(responses) if remaining == 0 else None
            if completed:
                self.write(completed)

        for message in messages:
            self._dispatch_message(message, collect, response_budget)

    def _cancel_notification(self, message: dict[str, object]) -> bool:
        if message.get("method") != "notifications/cancelled" or "id" in message:
            return False
        params = message.get("params")
        request_id = params.get("requestId") if is_object(params) else None
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return True
        with self.pending_lock:
            item = self.pending.get(request_id)
        if item is not None:
            future, cancellation, _ = item
            cancellation.set()
            future.cancel()
        return True

    @staticmethod
    def _progress_token(message: dict[str, object]) -> str | int | None:
        call_params = message.get("params")
        raw_meta = call_params.get("_meta") if is_object(call_params) else None
        raw_token = raw_meta.get("progressToken") if is_object(raw_meta) else None
        return (
            raw_token
            if not isinstance(raw_token, bool) and isinstance(raw_token, (str, int))
            else None
        )

    def _schedule_tool_call(
        self,
        message: dict[str, object],
        respond: Callable[[dict[str, object] | None], None],
        max_response_bytes: int | None,
    ) -> None:
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(
            request_id, (str, int, type(None))
        ):
            respond(_error(None, -32600, "Invalid Request: invalid id."))
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
            respond(_error(request_id, -32600, "Duplicate in-flight request id."))
            return
        if overloaded:
            respond(_error(request_id, -32000, "Too many in-flight requests."))
            return
        if duplicate_progress:
            respond(
                _error(
                    request_id,
                    -32602,
                    "progressToken is already active on another request.",
                )
            )
            return

        cancellation = threading.Event()
        future = self.executor.submit(
            self._execute, message, cancellation, max_response_bytes
        )
        with self.pending_lock:
            self.pending[request_id] = (future, cancellation, progress_token)
            if progress_token is not None:
                self.active_progress_tokens.add(progress_token)
        future.add_done_callback(partial(self._complete, request_id, respond))

    def dispatch(self, message: object) -> None:
        if is_array(message):
            self._dispatch_batch(message)
            return
        self._dispatch_message(message, self._respond)

    def _respond(self, response: dict[str, object] | None) -> None:
        if response is not None:
            self.write(response)

    def _dispatch_message(
        self,
        message: object,
        respond: Callable[[dict[str, object] | None], None],
        max_response_bytes: int | None = None,
    ) -> None:
        if not is_object(message):
            respond(_error(None, -32600, "Invalid Request: expected a JSON object."))
            return
        if self._cancel_notification(message):
            respond(None)
            return
        if message.get("method") == "tools/call" and "id" in message:
            self._schedule_tool_call(message, respond, max_response_bytes)
            return
        response = handle_request(message, self.session, self._send_progress)
        respond(response)


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
                message: object = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                runtime.write(_error(None, -32700, f"Invalid JSON: {exc}"))
                continue
            runtime.dispatch(message)
    finally:
        runtime.close()


def main() -> int:
    serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0
