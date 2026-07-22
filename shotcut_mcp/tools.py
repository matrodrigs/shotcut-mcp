"""MCP tool catalog and handlers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .errors import ToolError
from .platform import (
    compatibility_doctor,
    describe_service,
    detect_hardware_encoders,
    expand_path,
    list_services,
    open_in_shotcut,
    render_preview,
    render_preview_batch,
    status,
    summarize_media,
    validate_project_file,
)
from .project import (
    ProjectDocument,
    create_project,
    diagnose_color_workflow,
    diagnose_missing_media,
    edit_project,
    list_backups,
    plan_project_edit,
    render_project_contact_sheet,
    restore_backup,
)
from .render import (
    RENDER_PRESETS,
    cancel_render,
    list_render_jobs,
    render_status,
    start_render,
)

OPERATION_CATALOG: dict[str, dict[str, Any]] = {
    "add_track": {
        "required": ["kind"],
        "optional": ["name"],
        "notes": "kind: video|audio",
    },
    "remove_track": {"required": ["track"]},
    "update_track": {
        "required": ["track"],
        "optional": ["name", "locked", "hidden", "muted", "composite"],
    },
    "move_track": {"required": ["track", "before"]},
    "add_clip": {
        "required": ["track", "path"],
        "optional": [
            "position_frame",
            "mode",
            "in_frame",
            "out_frame",
            "in_seconds",
            "out_seconds",
            "caption",
            "image_duration_seconds",
        ],
        "notes": "mode: insert|overwrite; omitting position_frame appends to the end",
    },
    "add_generator": {
        "required": ["track", "generator", "duration_frames"],
        "optional": [
            "position_frame",
            "mode",
            "color",
            "text",
            "frequency",
            "level",
            "properties",
        ],
        "notes": "generator: color|text|tone|noise",
    },
    "remove_item": {"required": ["track", "item_index"], "optional": ["ripple"]},
    "trim_item": {
        "required": ["track", "item_index"],
        "optional": [
            "in_frame",
            "out_frame",
            "edge",
            "delta",
            "ripple",
            "ripple_markers",
            "ripple_tracks",
        ],
        "notes": "Legacy in/out remains compatible; edge+delta enables explicit ripple behavior.",
    },
    "roll_edit": {
        "required": ["track", "left_item_index", "delta"],
        "notes": "Moves one contiguous clip boundary without changing total duration.",
    },
    "slip_item": {
        "required": ["track", "item_index", "delta"],
        "notes": "Changes source in/out while preserving timeline position and duration.",
    },
    "slide_item": {
        "required": ["track", "item_index", "delta"],
        "notes": "Moves a clip between two contiguous clips without changing source or total duration.",
    },
    "split_item": {"required": ["track", "item_index", "offset_frame"]},
    "move_item": {
        "required": ["track", "item_index", "position_frame"],
        "optional": ["target_track", "mode", "ripple_source"],
    },
    "insert_gap": {
        "required": ["position_frame", "duration_frames"],
        "optional": ["tracks"],
        "notes": "tracks: list of names/ids or 'all'",
    },
    "remove_range": {
        "required": ["position_frame", "duration_frames"],
        "optional": ["tracks", "ripple"],
    },
    "add_transition": {
        "required": ["track", "left_item_index", "duration_frames"],
        "optional": ["service", "properties", "audio_crossfade", "name"],
        "notes": "Creates a nested Shotcut tractor between two adjacent clips.",
    },
    "remove_transition": {"required": ["track", "item_index"]},
    "add_filter": {
        "required": ["target", "service"],
        "optional": [
            "track",
            "item_index",
            "shotcut_filter",
            "in_frame",
            "out_frame",
            "properties",
        ],
        "notes": "target: project|track|clip; animations/keyframes use MLT property strings.",
    },
    "update_filter": {
        "required": ["filter_id"],
        "optional": ["enabled", "in_frame", "out_frame", "properties"],
        "notes": "Set a property to null to remove it.",
    },
    "remove_filter": {"required": ["filter_id"]},
    "set_notes": {"required": ["notes"]},
    "add_marker": {
        "required": ["start_frame"],
        "optional": ["end_frame", "text", "color"],
    },
    "remove_marker": {"required": ["marker_id"]},
    "set_subtitle_track": {
        "required": ["name", "items"],
        "optional": ["language", "burn_in", "style"],
        "notes": "Each item requires start_ms, end_ms, and text.",
    },
    "remove_subtitle_track": {"required": ["name"]},
    "relink_media": {
        "required": ["from", "to"],
        "optional": ["match_basename", "allow_multiple"],
        "notes": "match_basename requires a unique target unless allow_multiple=true.",
    },
    "set_profile": {
        "required": ["preserve_frame_numbers"],
        "optional": [
            "width",
            "height",
            "frame_rate_num",
            "frame_rate_den",
            "progressive",
            "sample_aspect_num",
            "sample_aspect_den",
            "display_aspect_num",
            "display_aspect_den",
            "colorspace",
        ],
        "notes": "Requires preserve_frame_numbers=true; existing positions are not resampled.",
    },
    "set_color_workflow": {
        "required": ["workflow"],
        "optional": ["processing_mode", "colorspace"],
        "notes": "workflow: sdr|hlg|pq; owns processing mode, transfer, and colorspace together.",
    },
    "set_clip_speed": {
        "required": ["track", "item_index", "speed"],
        "optional": ["pitch_compensation"],
        "notes": "Uses MLT timewarp; speed range is -100..-0.05 or 0.05..100.",
    },
    "set_clip_speed_map": {
        "required": ["track", "item_index", "keyframes"],
        "optional": ["image_mode", "pitch_compensation"],
        "notes": "Uses one owned timeremap link; positive monotonic speed maps only.",
    },
}


def capabilities(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "compatibility": {
            "shotcut": "26.6.25",
            "mlt": "7.40.x",
            "project_format": "MLT XML",
        },
        "transaction_guarantees": [
            "optimistic concurrency using SHA-256 revision",
            "single parse/write for up to 500 operations",
            "MCP lock file",
            "temporary-file MLT validation before replace",
            "atomic replace",
            "timestamped backup retention (20)",
            "unknown XML elements and properties preserved",
        ],
        "operations": OPERATION_CATALOG,
        "render_presets": RENDER_PRESETS,
        "workflow": [
            "run shotcut_doctor after installing or upgrading Shotcut",
            "inspect_project to obtain revision and current item indexes",
            "optionally list_mlt_services/describe_mlt_service",
            "edit_project with expected_revision and one batch of operations",
            "render_preview or validate_project",
            "start_render; render_status is optional for monitoring and logs",
        ],
    }


def inspect_project(arguments: dict[str, Any]) -> dict[str, Any]:
    return ProjectDocument.load(expand_path(arguments.get("path", ""))).snapshot()


def validate_project(arguments: dict[str, Any]) -> dict[str, Any]:
    path = expand_path(arguments.get("path", ""))
    timeout = arguments.get("timeout_seconds", 30)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 300
    ):
        raise ToolError("timeout_seconds must be an integer between 1 and 300.")
    return {
        "project": ProjectDocument.load(path).snapshot(),
        **validate_project_file(path, timeout),
    }


def render_preview_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    frame = arguments.get("frame", 0)
    if isinstance(frame, bool) or not isinstance(frame, int):
        raise ToolError("frame must be an integer.")
    overwrite = arguments.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ToolError("overwrite must be a boolean.")
    return render_preview(
        expand_path(arguments.get("project_path", "")),
        expand_path(arguments.get("output_path", "")),
        frame,
        overwrite,
    )


def render_preview_batch_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_requests = arguments.get("requests")
    if not isinstance(raw_requests, list) or not 1 <= len(raw_requests) <= 64:
        raise ToolError("requests must contain between 1 and 64 frame/output pairs.")
    requests: list[tuple[int, Any]] = []
    for index, item in enumerate(raw_requests):
        if not isinstance(item, dict):
            raise ToolError(f"requests[{index}] must be an object.")
        frame = item.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
            raise ToolError(f"requests[{index}].frame must be a non-negative integer.")
        requests.append((frame, expand_path(item.get("output_path", ""))))
    overwrite = arguments.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ToolError("overwrite must be a boolean.")
    return render_preview_batch(
        expand_path(arguments.get("project_path", "")), requests, overwrite
    )


def render_contact_sheet_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    return render_project_contact_sheet(arguments)


def open_in_shotcut_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    fullscreen = arguments.get("fullscreen", False)
    if not isinstance(fullscreen, bool):
        raise ToolError("fullscreen must be a boolean.")
    return open_in_shotcut(expand_path(arguments.get("path", "")), fullscreen)


def list_backups_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    return list_backups(expand_path(arguments.get("project_path", "")))


def _object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


PATH = {"type": "string", "description": "Absolute or relative local path."}
TRACK = {
    "type": "string",
    "description": "Track name or id returned by inspect_project.",
}
OP_NAMES = list(OPERATION_CATALOG)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "shotcut_status",
        "title": "Check Shotcut status",
        "description": "Locates Shotcut, Melt, ffprobe, and ffmpeg and reports their versions.",
        "inputSchema": _object_schema({}),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "shotcut_doctor",
        "title": "Check Shotcut compatibility",
        "description": (
            "Verifies the validated Shotcut/MLT versions, repository startup, "
            "RNNoise link/filter availability, and active path policy."
        ),
        "inputSchema": _object_schema({}),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "shotcut_capabilities",
        "title": "Get editing capabilities",
        "description": "Returns the complete catalog of operations, parameters, and transactional guarantees.",
        "inputSchema": _object_schema({}),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "probe_media",
        "title": "Probe media",
        "description": "Reads duration, codecs, resolution, frame rate, and audio with per-file caching.",
        "inputSchema": _object_schema({"path": PATH}, ["path"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "inspect_project",
        "title": "Inspect complete project",
        "description": "Returns the SHA-256 revision, profile, tracks, items, filters, markers, subtitles, and resources.",
        "inputSchema": _object_schema({"path": PATH}, ["path"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "plan_project_edit",
        "title": "Plan project edit",
        "description": (
            "Applies operations in memory, validates the candidate with MLT, and returns "
            "a prospective snapshot and bounded unified diff without changing the project."
        ),
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "expected_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "properties": {"op": {"type": "string", "enum": OP_NAMES}},
                        "required": ["op"],
                        "additionalProperties": True,
                    },
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                "max_diff_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5000,
                    "default": 2000,
                },
            },
            ["project_path", "expected_revision", "operations"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "create_project",
        "title": "Create multitrack Shotcut project",
        "description": "Creates Shotcut 26.6 MLT XML with a background, V1, additional tracks, and optional clips.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "width": {"type": "integer", "minimum": 16, "default": 1920},
                "height": {"type": "integer", "minimum": 16, "default": 1080},
                "fps_num": {"type": "integer", "minimum": 1, "default": 30},
                "fps_den": {"type": "integer", "minimum": 1, "default": 1},
                "notes": {"type": "string"},
                "tracks": {"type": "array", "items": {"type": "object"}},
                "clips": {"type": "array", "items": {"type": "object"}},
                "overwrite": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            ["project_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "edit_project",
        "title": "Edit project transactionally",
        "description": (
            "Applies up to 500 operations in one atomic write. Obtain the revision from inspect_project "
            "and consult shotcut_capabilities for each operation's parameters."
        ),
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "expected_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "properties": {"op": {"type": "string", "enum": OP_NAMES}},
                        "required": ["op"],
                        "additionalProperties": True,
                    },
                },
                "force": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            ["project_path", "operations"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "list_mlt_services",
        "title": "List MLT services",
        "description": "Lists filters, transitions, producers, consumers, or links installed with Shotcut.",
        "inputSchema": _object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["filter", "transition", "producer", "consumer", "link"],
                }
            },
            ["kind"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "describe_mlt_service",
        "title": "Describe MLT service",
        "description": "Queries the official properties and metadata exposed by the local MLT installation.",
        "inputSchema": _object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["filter", "transition", "producer", "consumer", "link"],
                },
                "name": {"type": "string"},
            },
            ["kind", "name"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "validate_project",
        "title": "Validate project with MLT",
        "description": "Parses the XML and processes the first frame with the local Melt installation.",
        "inputSchema": _object_schema(
            {
                "path": PATH,
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "default": 30,
                },
            },
            ["path"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "render_preview",
        "title": "Render preview frame",
        "description": "Renders one PNG frame to visually verify an edit.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "output_path": PATH,
                "frame": {"type": "integer", "minimum": 0, "default": 0},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["project_path", "output_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "diagnose_color_workflow",
        "title": "Diagnose project color workflow",
        "description": "Reports normalized source color facts and Shotcut 26.6 HDR compatibility issues.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "output_codec": {"type": "string"},
                "hdr10_metadata": {"type": "boolean", "default": False},
            },
            ["project_path"],
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "diagnose_missing_media",
        "title": "Find missing-media candidates",
        "description": "Searches authorized roots with bounded Shotcut-hash and basename scoring; never relinks automatically.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "search_roots": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": PATH,
                },
                "max_depth": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 16,
                    "default": 6,
                },
                "max_files": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20000,
                    "default": 5000,
                },
                "max_candidates_per_resource": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                },
                "max_hash_bytes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1073741824,
                    "default": 268435456,
                },
                "max_probe_candidates": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 256,
                    "default": 128,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 30,
                },
                "visual_output_path": PATH,
                "visual_columns": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 4,
                },
                "visual_cell_width": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 1920,
                    "default": 320,
                },
                "overwrite_visual": {"type": "boolean", "default": False},
            },
            ["project_path", "search_roots"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "render_preview_batch",
        "title": "Render preview frames in batch",
        "description": "Renders up to 64 exact frames with bounded per-output results.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "requests": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": _object_schema(
                        {
                            "frame": {"type": "integer", "minimum": 0},
                            "output_path": PATH,
                        },
                        ["frame", "output_path"],
                    ),
                },
                "overwrite": {"type": "boolean", "default": False},
            },
            ["project_path", "requests"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "render_contact_sheet",
        "title": "Render a contact sheet",
        "description": "Renders exact or evenly sampled frames into one atomically promoted PNG/JPEG.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "output_path": PATH,
                "frames": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": {"type": "integer", "minimum": 0},
                },
                "sample_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 64,
                    "default": 12,
                },
                "columns": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 4,
                },
                "cell_width": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 1920,
                    "default": 320,
                },
                "overwrite": {"type": "boolean", "default": False},
            },
            ["project_path", "output_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "detect_hardware_encoders",
        "title": "Detect usable hardware encoders",
        "description": "Lists advertised FFmpeg encoders and smoke-tests each OS-appropriate candidate.",
        "inputSchema": _object_schema(
            {"refresh": {"type": "boolean", "default": False}}
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "open_in_shotcut",
        "title": "Open in Shotcut",
        "description": "Opens a project, media file, or folder in the Shotcut interface.",
        "inputSchema": _object_schema(
            {"path": PATH, "fullscreen": {"type": "boolean", "default": False}},
            ["path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "start_render",
        "title": "Start render",
        "description": "Exports in the background and returns a monitorable job_id.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "output_path": PATH,
                "preset": {
                    "type": "string",
                    "enum": list(RENDER_PRESETS),
                    "default": "h264-high",
                },
                "consumer_properties": {"type": "object", "additionalProperties": True},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["project_path", "output_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "render_status",
        "title": "Get render status",
        "description": "Returns the status, progress, log, and output size.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_render_jobs",
        "title": "List render history",
        "description": "Returns bounded newest-first durable render summaries with cursor pagination.",
        "inputSchema": _object_schema(
            {
                "status": {
                    "type": "string",
                    "enum": [
                        "queued",
                        "running",
                        "cancelled",
                        "completed",
                        "failed",
                        "orphaned",
                        "promotion_failed",
                    ],
                },
                "cursor": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            }
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "cancel_render",
        "title": "Cancel render",
        "description": "Requests cancellation of an active supervised render, including after an MCP restart.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_project_backups",
        "title": "List project backups",
        "description": "Lists automatic revisions available for recovery.",
        "inputSchema": _object_schema({"project_path": PATH}, ["project_path"]),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "restore_project_backup",
        "title": "Restore project backup",
        "description": "Validates and restores a backup after first saving a copy of the current version.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "backup_path": PATH,
                "expected_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "force": {"type": "boolean", "default": False},
            },
            ["project_path", "backup_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
]


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "shotcut_status": lambda _: status(),
    "shotcut_doctor": lambda _: compatibility_doctor(),
    "shotcut_capabilities": capabilities,
    "probe_media": lambda arguments: summarize_media(
        expand_path(arguments.get("path", ""))
    ),
    "inspect_project": inspect_project,
    "diagnose_color_workflow": diagnose_color_workflow,
    "diagnose_missing_media": diagnose_missing_media,
    "plan_project_edit": plan_project_edit,
    "create_project": create_project,
    "edit_project": edit_project,
    "list_mlt_services": lambda arguments: list_services(arguments.get("kind", "")),
    "describe_mlt_service": lambda arguments: describe_service(
        arguments.get("kind", ""), arguments.get("name", "")
    ),
    "validate_project": validate_project,
    "render_preview": render_preview_tool,
    "render_preview_batch": render_preview_batch_tool,
    "render_contact_sheet": render_contact_sheet_tool,
    "detect_hardware_encoders": lambda arguments: detect_hardware_encoders(
        arguments.get("refresh", False)
    ),
    "open_in_shotcut": open_in_shotcut_tool,
    "start_render": start_render,
    "render_status": lambda arguments: render_status(arguments.get("job_id", "")),
    "list_render_jobs": list_render_jobs,
    "cancel_render": lambda arguments: cancel_render(arguments.get("job_id", "")),
    "list_project_backups": list_backups_tool,
    "restore_project_backup": restore_backup,
}
