"""MCP tool catalog and handlers."""

from __future__ import annotations

from typing import Any, Callable

from .errors import ToolError
from .platform import (
    describe_service,
    expand_path,
    list_services,
    open_in_shotcut,
    render_preview,
    status,
    summarize_media,
    validate_project_file,
)
from .project import (
    ProjectDocument,
    create_project,
    edit_project,
    list_backups,
    restore_backup,
)
from .render import RENDER_PRESETS, cancel_render, render_status, start_render


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
        "optional": ["in_frame", "out_frame"],
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
}


def capabilities(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "compatibility": {
            "shotcut": "26.2.26",
            "mlt": "7.37.x",
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
            "inspect_project to obtain revision and current item indexes",
            "optionally list_mlt_services/describe_mlt_service",
            "edit_project with expected_revision and one batch of operations",
            "render_preview or validate_project",
            "start_render and poll render_status",
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
        "name": "create_project",
        "title": "Create multitrack Shotcut project",
        "description": "Creates Shotcut 26.2 MLT XML with a background, V1, additional tracks, and optional clips.",
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
            "destructiveHint": False,
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
            "openWorldHint": False,
        },
    },
    {
        "name": "list_mlt_services",
        "title": "List MLT services",
        "description": "Lists filters, transitions, producers, or consumers installed with Shotcut.",
        "inputSchema": _object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["filter", "transition", "producer", "consumer"],
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
                    "enum": ["filter", "transition", "producer", "consumer"],
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
            "openWorldHint": False,
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
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
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
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
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
        "name": "cancel_render",
        "title": "Cancel render",
        "description": "Stops an active render started in this MCP session.",
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
            "openWorldHint": False,
        },
    },
]


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "shotcut_status": lambda _: status(),
    "shotcut_capabilities": capabilities,
    "probe_media": lambda arguments: summarize_media(
        expand_path(arguments.get("path", ""))
    ),
    "inspect_project": inspect_project,
    "create_project": create_project,
    "edit_project": edit_project,
    "list_mlt_services": lambda arguments: list_services(arguments.get("kind", "")),
    "describe_mlt_service": lambda arguments: describe_service(
        arguments.get("kind", ""), arguments.get("name", "")
    ),
    "validate_project": validate_project,
    "render_preview": render_preview_tool,
    "open_in_shotcut": open_in_shotcut_tool,
    "start_render": start_render,
    "render_status": lambda arguments: render_status(arguments.get("job_id", "")),
    "cancel_render": lambda arguments: cancel_render(arguments.get("job_id", "")),
    "list_project_backups": list_backups_tool,
    "restore_project_backup": restore_backup,
}
