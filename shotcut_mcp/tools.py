"""MCP tool catalog and handlers."""

from __future__ import annotations

import copy
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

OPERATION_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "kind": {
        "type": "string",
        "enum": ["video", "audio"],
        "description": "Track kind.",
    },
    "name": {"type": "string", "description": "Human-readable name."},
    "track": {
        "type": "string",
        "description": "Track name or id from inspect_project.",
    },
    "before": {
        "type": "string",
        "description": "Track name or id that should follow the moved track.",
    },
    "locked": {
        "type": "boolean",
        "description": "Whether the track is locked in Shotcut.",
    },
    "hidden": {
        "type": "boolean",
        "description": "Whether video on the track is hidden.",
    },
    "muted": {"type": "boolean", "description": "Whether audio on the track is muted."},
    "composite": {
        "type": "boolean",
        "description": "Whether video compositing is enabled.",
    },
    "path": {"type": "string", "description": "Authorized local media path."},
    "position_frame": {
        "type": "integer",
        "minimum": 0,
        "description": "Zero-based timeline frame.",
    },
    "mode": {
        "type": "string",
        "enum": ["insert", "overwrite"],
        "description": "Timeline placement mode.",
    },
    "in_frame": {
        "type": "integer",
        "minimum": 0,
        "description": "Inclusive source or filter in frame.",
    },
    "out_frame": {
        "type": "integer",
        "minimum": 0,
        "description": "Inclusive source or filter out frame.",
    },
    "in_seconds": {
        "type": "number",
        "minimum": 0,
        "description": "Inclusive source in point in seconds.",
    },
    "out_seconds": {
        "type": "number",
        "minimum": 0,
        "description": "Inclusive source out point in seconds.",
    },
    "caption": {"type": "string", "description": "Shotcut clip caption."},
    "image_duration_seconds": {
        "type": "number",
        "minimum": 0,
        "description": "Still-image duration in seconds.",
    },
    "generator": {
        "type": "string",
        "enum": ["color", "text", "tone", "noise"],
        "description": "Generator kind.",
    },
    "duration_frames": {
        "type": "integer",
        "minimum": 1,
        "description": "Duration in timeline frames.",
    },
    "color": {
        "type": "string",
        "description": "Generator color or #RRGGBB marker color.",
    },
    "text": {"type": "string", "description": "Visible text or marker label."},
    "frequency": {
        "type": "number",
        "minimum": 1,
        "description": "Tone frequency in hertz.",
    },
    "level": {"type": "number", "description": "Generator audio level."},
    "properties": {
        "type": "object",
        "description": "MLT properties; animations use MLT property strings.",
        "additionalProperties": True,
    },
    "item_index": {
        "type": "integer",
        "minimum": 0,
        "description": "Zero-based item index from inspect_project.",
    },
    "ripple": {
        "type": "boolean",
        "description": "Close or create timeline space after the edit.",
    },
    "edge": {
        "type": "string",
        "enum": ["start", "end"],
        "description": "Clip edge adjusted by delta.",
    },
    "delta": {"type": "integer", "description": "Signed frame adjustment."},
    "ripple_markers": {
        "type": "boolean",
        "description": "Move affected markers with a ripple trim.",
    },
    "left_item_index": {
        "type": "integer",
        "minimum": 0,
        "description": "Index of the item left of the edit point.",
    },
    "offset_frame": {
        "type": "integer",
        "minimum": 1,
        "description": "Frame offset inside the selected item.",
    },
    "target_track": {"type": "string", "description": "Destination track name or id."},
    "ripple_source": {
        "type": "boolean",
        "description": "Close the source gap after moving an item.",
    },
    "tracks": {
        "type": ["array", "string"],
        "items": {"type": "string"},
        "description": "Track names/ids or the string 'all'.",
    },
    "service": {
        "type": "string",
        "description": "Installed MLT service name; query list_mlt_services when unfamiliar.",
    },
    "audio_crossfade": {
        "type": "boolean",
        "description": "Add the paired audio crossfade behavior.",
    },
    "target": {
        "type": "string",
        "enum": ["project", "track", "clip"],
        "description": "Filter host.",
    },
    "shotcut_filter": {
        "type": "string",
        "description": "Optional Shotcut filter identifier.",
    },
    "filter_id": {
        "type": "string",
        "description": "Filter id returned by inspect_project or add_filter.",
    },
    "enabled": {"type": "boolean", "description": "Enable or disable the filter."},
    "notes": {
        "type": "string",
        "description": "Project notes; an empty string clears them.",
    },
    "start_frame": {
        "type": "integer",
        "minimum": 0,
        "description": "Marker start frame.",
    },
    "end_frame": {"type": "integer", "minimum": 0, "description": "Marker end frame."},
    "marker_id": {
        "type": "string",
        "description": "Marker id returned by inspect_project or add_marker.",
    },
    "language": {"type": "string", "description": "Subtitle language code."},
    "items": {
        "type": "array",
        "description": "Subtitle cues in milliseconds.",
        "items": {
            "type": "object",
            "properties": {
                "start_ms": {"type": "integer", "minimum": 0},
                "end_ms": {"type": "integer", "minimum": 1},
                "text": {"type": "string"},
            },
            "required": ["start_ms", "end_ms", "text"],
            "additionalProperties": False,
        },
    },
    "burn_in": {
        "type": "boolean",
        "description": "Render subtitles visibly into video output.",
    },
    "style": {
        "type": "object",
        "description": "MLT subtitle style overrides.",
        "additionalProperties": True,
    },
    "from": {
        "type": "string",
        "description": "Missing resource path or basename to replace.",
    },
    "to": {"type": "string", "description": "Authorized existing replacement path."},
    "match_basename": {
        "type": "boolean",
        "description": "Match the from value as a basename instead of a full path.",
    },
    "allow_multiple": {
        "type": "boolean",
        "description": "Permit one basename to relink multiple resources.",
    },
    "preserve_frame_numbers": {
        "type": "boolean",
        "description": "Must be true; existing timeline and marker frame numbers are preserved.",
    },
    "width": {
        "type": "integer",
        "minimum": 16,
        "description": "Profile width in pixels.",
    },
    "height": {
        "type": "integer",
        "minimum": 16,
        "description": "Profile height in pixels.",
    },
    "frame_rate_num": {
        "type": "integer",
        "minimum": 1,
        "description": "Frame-rate numerator.",
    },
    "frame_rate_den": {
        "type": "integer",
        "minimum": 1,
        "description": "Frame-rate denominator.",
    },
    "progressive": {
        "type": "boolean",
        "description": "Whether the profile is progressive.",
    },
    "sample_aspect_num": {
        "type": "integer",
        "minimum": 1,
        "description": "Sample-aspect numerator.",
    },
    "sample_aspect_den": {
        "type": "integer",
        "minimum": 1,
        "description": "Sample-aspect denominator.",
    },
    "display_aspect_num": {
        "type": "integer",
        "minimum": 1,
        "description": "Display-aspect numerator.",
    },
    "display_aspect_den": {
        "type": "integer",
        "minimum": 1,
        "description": "Display-aspect denominator.",
    },
    "colorspace": {
        "type": "integer",
        "enum": [601, 709, 2020],
        "description": "MLT colorspace code.",
    },
    "workflow": {
        "type": "string",
        "enum": ["sdr", "hlg", "pq"],
        "description": "Project color workflow.",
    },
    "processing_mode": {
        "type": "string",
        "enum": ["Native8Cpu", "Native10Cpu", "Linear10Cpu", "Linear10GpuCpu"],
        "description": "Shotcut processing mode compatible with the workflow.",
    },
    "speed": {
        "type": "number",
        "description": "Playback multiplier: -100..-0.05 or 0.05..100.",
    },
    "pitch_compensation": {
        "type": "boolean",
        "description": "Preserve perceived audio pitch.",
    },
    "keyframes": {
        "type": "array",
        "minItems": 2,
        "maxItems": 64,
        "description": "Strictly increasing positive speed points; the first frame must be 0.",
        "items": {
            "type": "object",
            "properties": {
                "frame": {"type": "integer", "minimum": 0},
                "speed": {"type": "number", "minimum": 0.01, "maximum": 100},
            },
            "required": ["frame", "speed"],
            "additionalProperties": False,
        },
    },
    "image_mode": {
        "type": "string",
        "enum": ["blend", "nearest"],
        "description": "Timeremap frame interpolation mode.",
    },
}

OPERATION_EXAMPLES: dict[str, dict[str, Any]] = {
    "add_track": {"op": "add_track", "kind": "video", "name": "Titles"},
    "remove_track": {"op": "remove_track", "track": "V2"},
    "update_track": {"op": "update_track", "track": "A1", "muted": True},
    "move_track": {"op": "move_track", "track": "V2", "before": "V1"},
    "add_clip": {
        "op": "add_clip",
        "track": "V1",
        "path": "C:/media/clip.mp4",
        "position_frame": 0,
        "mode": "insert",
    },
    "add_generator": {
        "op": "add_generator",
        "track": "V1",
        "generator": "text",
        "duration_frames": 90,
        "text": "Opening title",
    },
    "remove_item": {
        "op": "remove_item",
        "track": "V1",
        "item_index": 0,
        "ripple": True,
    },
    "trim_item": {
        "op": "trim_item",
        "track": "V1",
        "item_index": 0,
        "edge": "end",
        "delta": -12,
        "ripple": True,
    },
    "roll_edit": {"op": "roll_edit", "track": "V1", "left_item_index": 0, "delta": 6},
    "slip_item": {"op": "slip_item", "track": "V1", "item_index": 0, "delta": 12},
    "slide_item": {"op": "slide_item", "track": "V1", "item_index": 1, "delta": -6},
    "split_item": {
        "op": "split_item",
        "track": "V1",
        "item_index": 0,
        "offset_frame": 30,
    },
    "move_item": {
        "op": "move_item",
        "track": "V1",
        "item_index": 0,
        "position_frame": 90,
        "target_track": "V2",
    },
    "insert_gap": {
        "op": "insert_gap",
        "position_frame": 90,
        "duration_frames": 30,
        "tracks": "all",
    },
    "remove_range": {
        "op": "remove_range",
        "position_frame": 90,
        "duration_frames": 30,
        "tracks": ["V1", "A1"],
        "ripple": True,
    },
    "add_transition": {
        "op": "add_transition",
        "track": "V1",
        "left_item_index": 0,
        "duration_frames": 12,
        "audio_crossfade": True,
    },
    "remove_transition": {"op": "remove_transition", "track": "V1", "item_index": 1},
    "add_filter": {
        "op": "add_filter",
        "target": "clip",
        "track": "V1",
        "item_index": 0,
        "service": "brightness",
        "properties": {"level": "0.1"},
    },
    "update_filter": {"op": "update_filter", "filter_id": "filter0", "enabled": False},
    "remove_filter": {"op": "remove_filter", "filter_id": "filter0"},
    "set_notes": {"op": "set_notes", "notes": "Rough cut approved"},
    "add_marker": {
        "op": "add_marker",
        "start_frame": 0,
        "text": "Intro",
        "color": "#00A0FF",
    },
    "remove_marker": {"op": "remove_marker", "marker_id": "0"},
    "set_subtitle_track": {
        "op": "set_subtitle_track",
        "name": "Portuguese",
        "language": "por",
        "items": [{"start_ms": 0, "end_ms": 1500, "text": "Olá"}],
        "burn_in": True,
    },
    "remove_subtitle_track": {"op": "remove_subtitle_track", "name": "Portuguese"},
    "relink_media": {
        "op": "relink_media",
        "from": "C:/old/clip.mp4",
        "to": "C:/media/clip.mp4",
    },
    "set_profile": {
        "op": "set_profile",
        "preserve_frame_numbers": True,
        "width": 1920,
        "height": 1080,
        "frame_rate_num": 30,
        "frame_rate_den": 1,
    },
    "set_color_workflow": {"op": "set_color_workflow", "workflow": "sdr"},
    "set_clip_speed": {
        "op": "set_clip_speed",
        "track": "V1",
        "item_index": 0,
        "speed": 1.25,
        "pitch_compensation": True,
    },
    "set_clip_speed_map": {
        "op": "set_clip_speed_map",
        "track": "V1",
        "item_index": 0,
        "keyframes": [{"frame": 0, "speed": 1.0}, {"frame": 60, "speed": 2.0}],
    },
}


def _operation_details(name: str) -> dict[str, Any]:
    summary = OPERATION_CATALOG[name]
    fields = [*summary.get("required", []), *summary.get("optional", [])]
    schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": [name]},
            **{field: OPERATION_FIELD_SCHEMAS[field] for field in fields},
        },
        "required": ["op", *summary.get("required", [])],
        "additionalProperties": False,
    }
    return {**summary, "schema": schema, "example": OPERATION_EXAMPLES[name]}


def capabilities(arguments: dict[str, Any]) -> dict[str, Any]:
    requested = arguments.get("operation")
    if requested is not None and (
        not isinstance(requested, str) or requested not in OPERATION_CATALOG
    ):
        raise ToolError(f"Unknown edit operation: {requested}")
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
        "operations": (
            {requested: _operation_details(requested)}
            if isinstance(requested, str)
            else OPERATION_CATALOG
        ),
        "operation_query": (
            "Pass operation to shotcut_capabilities for its complete schema and example."
        ),
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
    raw_output = arguments.get("output_path")
    if raw_output is not None and not isinstance(raw_output, str):
        raise ToolError("output_path must be a string when provided.")
    return render_preview(
        expand_path(arguments.get("project_path", "")),
        expand_path(raw_output) if isinstance(raw_output, str) else None,
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
        "description": "Use for a quick readiness check. Locates Shotcut, Melt, FFprobe, and FFmpeg and reports their versions; use shotcut_doctor for compatibility diagnosis.",
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
            "Use after installation, upgrade, or a setup failure. Verifies validated "
            "Shotcut/MLT versions, repository startup, RNNoise, and path policy."
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
        "description": "Use before an unfamiliar edit. Returns the operation catalog and transactional guarantees; pass operation for its complete schema and example.",
        "inputSchema": _object_schema(
            {
                "operation": {
                    "type": "string",
                    "enum": OP_NAMES,
                    "description": "Optional edit operation to describe in full.",
                }
            }
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "probe_media",
        "title": "Probe media",
        "description": "Use to understand a source file before editing. Reads duration, codecs, resolution, frame rate, color, and audio with per-file caching.",
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
        "description": "Use first to understand a timeline or before planning, editing, or restoring. Returns structural state and the SHA-256 revision; use render_contact_sheet for visual review.",
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
            "Use for a dry run, uncertain edit, or user review before committing. Applies "
            "operations in memory, validates with MLT, and returns a snapshot and diff "
            "without changing the project."
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
        "description": "Use to create a new editable Shotcut 26.6 project. Builds MLT XML with a background, V1, optional add_track-shaped tracks, and add_clip-shaped clips.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "width": {"type": "integer", "minimum": 16, "default": 1920},
                "height": {"type": "integer", "minimum": 16, "default": 1080},
                "fps_num": {"type": "integer", "minimum": 1, "default": 30},
                "fps_den": {"type": "integer", "minimum": 1, "default": 1},
                "notes": {"type": "string"},
                "tracks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            field: OPERATION_FIELD_SCHEMAS[field]
                            for field in ("kind", "name")
                        },
                        "required": ["kind"],
                        "additionalProperties": False,
                    },
                },
                "clips": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            field: OPERATION_FIELD_SCHEMAS[field]
                            for field in (
                                "track",
                                "path",
                                "position_frame",
                                "mode",
                                "in_frame",
                                "out_frame",
                                "in_seconds",
                                "out_seconds",
                                "caption",
                                "image_duration_seconds",
                            )
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
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
            "Use only after inspect_project. Applies up to 500 related operations in one "
            "validated atomic write; pass expected_revision and re-inspect on conflicts "
            "instead of using force."
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
        "description": "Use when an edit needs an unfamiliar native MLT effect or transition. Lists services of one kind installed with Shotcut.",
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
        "description": "Use after list_mlt_services to learn one installed service's accepted properties and local MLT metadata.",
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
        "description": "Use to check technical MLT validity, not visual quality. Parses the XML and processes the first frame with local Melt.",
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
        "description": "Use to show or inspect one specific moment. Renders one PNG and returns its path; omit output_path for a bounded server-managed preview.",
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "output_path": PATH,
                "frame": {"type": "integer", "minimum": 0, "default": 0},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["project_path"],
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
        "description": "Use for washed-out colors, HDR/SDR questions, or export compatibility. Reports normalized source color facts and Shotcut 26.6 issues.",
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
        "description": "Use when media is missing or offline. Searches authorized roots and ranks candidates; never relinks automatically, so let the user choose first.",
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
        "description": "Use when exact frames are needed as separate image files. Renders up to 64 requested frames; use render_contact_sheet for one overview image.",
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
        "description": "Use for 'show me the edit', visual review, or a timeline overview. Renders exact frames or 12 evenly sampled frames into one image; omit output_path for managed output.",
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
            ["project_path"],
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
        "description": "Use when choosing or troubleshooting hardware export. Lists advertised FFmpeg encoders and smoke-tests OS-appropriate candidates.",
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
        "description": "Use only when the user asks to open the GUI. Opens a project, media file, or folder in Shotcut; MCP edits do not require this.",
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
        "description": "Use when the user asks to export. Starts a durable background render and returns job_id; prefer a named preset over consumer_properties.",
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
        "description": "Use when job_id is known to report render state, progress, log tail, and output size. Use list_render_jobs when the id is unknown.",
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
        "description": "Use for 'the latest render', render history, or when job_id is unknown. Returns bounded newest-first durable summaries.",
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
        "description": "Use only when the user asks to stop an active render. Requests supervised cancellation, including after an MCP restart.",
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
        "description": "Use before undo or recovery. Lists only backups owned by this project, including paths, revisions, sizes, and timestamps.",
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
        "description": "Use after list_project_backups and confirmation of the selected revision. Validates and atomically restores it while backing up the current project.",
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

PARAMETER_DESCRIPTIONS: dict[str, str] = {
    "path": "Existing authorized local project, media, folder, or backup path.",
    "project_path": "Existing authorized Shotcut .mlt or .xml project path, except when create_project creates it.",
    "output_path": "Authorized destination path. Omit only for single-frame or contact-sheet previews to use bounded managed output.",
    "operation": "Edit operation whose complete schema and example should be returned.",
    "expected_revision": "SHA-256 revision returned by inspect_project. Required unless force is explicitly authorized.",
    "operations": "One or more related edit operations. Query shotcut_capabilities for unfamiliar operation schemas.",
    "timeout_seconds": "Bounded timeout for local MLT processing.",
    "max_diff_lines": "Maximum unified-diff lines returned by a dry run; zero suppresses diff text.",
    "width": "Project or contact-sheet width in pixels, depending on the tool.",
    "height": "Project height in pixels.",
    "fps_num": "Project frame-rate numerator.",
    "fps_den": "Project frame-rate denominator.",
    "notes": "Initial project notes.",
    "tracks": "Initial tracks using add_track fields; V1 already exists.",
    "clips": "Initial clips using add_clip fields; track defaults to V1.",
    "overwrite": "Replace an existing destination only when the user explicitly authorized it.",
    "force": "Bypass revision protection only with explicit user authorization; never retry conflicts with force automatically.",
    "kind": "MLT service kind to list or describe.",
    "name": "Exact service name returned by list_mlt_services.",
    "frame": "Zero-based project frame to render.",
    "output_codec": "Optional intended export codec used to assess color compatibility.",
    "hdr10_metadata": "Whether the intended export requires HDR10 metadata.",
    "search_roots": "One to eight authorized roots searched for missing-media candidates.",
    "max_depth": "Maximum directory depth below each search root.",
    "max_files": "Maximum files examined across all search roots.",
    "max_candidates_per_resource": "Maximum ranked candidates returned for each missing resource.",
    "max_hash_bytes": "Largest candidate file eligible for Shotcut hash verification.",
    "max_probe_candidates": "Maximum candidates inspected with FFprobe.",
    "visual_output_path": "Optional PNG/JPEG destination for a missing-media candidate sheet.",
    "visual_columns": "Columns in the optional missing-media candidate sheet.",
    "visual_cell_width": "Width of each optional missing-media candidate cell in pixels.",
    "overwrite_visual": "Replace an existing candidate sheet only with explicit authorization.",
    "requests": "Exact frame/output-path pairs; every output path must be unique.",
    "frames": "Exact frames for the contact sheet. When present, sample_count is ignored.",
    "sample_count": "Evenly sampled frame count used only when frames is omitted.",
    "columns": "Number of contact-sheet columns.",
    "cell_width": "Width of each contact-sheet cell in pixels.",
    "refresh": "Ignore cached encoder results and run smoke tests again.",
    "fullscreen": "Ask Shotcut to open fullscreen.",
    "preset": "Named safe export preset returned by shotcut_capabilities.",
    "consumer_properties": "Advanced MLT avformat properties; prefer a named preset unless the user supplied exact requirements.",
    "job_id": "Durable render identifier returned by start_render or list_render_jobs.",
    "status": "Optional render state filter.",
    "cursor": "Opaque cursor returned by the preceding render-history page.",
    "limit": "Maximum render summaries returned.",
    "backup_path": "Exact backup path returned by list_project_backups for this project.",
    "op": "Edit operation name.",
}


def _describe_input_schema(schema: dict[str, Any]) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            if not isinstance(child, dict):
                continue
            description = PARAMETER_DESCRIPTIONS.get(name)
            if description and (
                "description" not in child
                or child.get("description") == PATH["description"]
            ):
                child["description"] = description
            _describe_input_schema(child)
    items = schema.get("items")
    if isinstance(items, dict):
        _describe_input_schema(items)


def _clone_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy a schema without preserving aliases between reused field dictionaries."""

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            result[key] = {
                name: _clone_schema(child) if isinstance(child, dict) else child
                for name, child in value.items()
            }
        elif key == "items" and isinstance(value, dict):
            result[key] = _clone_schema(value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _result_schema(properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": True,
    }


OBJECT = {"type": "object"}
ARRAY = {"type": "array"}
STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}

OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "shotcut_status": _result_schema(
        {
            "ready": BOOLEAN,
            "shotcut": OBJECT,
            "melt": OBJECT,
            "ffmpeg": OBJECT,
            "ffprobe": OBJECT,
        }
    ),
    "shotcut_doctor": _result_schema(
        {"compatible": BOOLEAN, "issues": ARRAY, "path_policy": OBJECT}
    ),
    "shotcut_capabilities": _result_schema(
        {
            "compatibility": OBJECT,
            "transaction_guarantees": ARRAY,
            "operations": OBJECT,
            "operation_query": STRING,
            "render_presets": OBJECT,
            "workflow": ARRAY,
        }
    ),
    "probe_media": _result_schema(
        {"path": STRING, "duration_seconds": {"type": "number"}, "streams": ARRAY}
    ),
    "inspect_project": _result_schema(
        {
            "path": STRING,
            "revision": STRING,
            "profile": OBJECT,
            "duration_frames": INTEGER,
            "tracks": ARRAY,
            "filters": ARRAY,
            "markers": ARRAY,
            "subtitles": ARRAY,
            "resources": ARRAY,
            "missing_resources": ARRAY,
        }
    ),
    "plan_project_edit": _result_schema(
        {
            "planned": BOOLEAN,
            "changed": BOOLEAN,
            "project_path": STRING,
            "base_revision": STRING,
            "prospective_revision": STRING,
            "operation_results": ARRAY,
            "validation": OBJECT,
            "project": OBJECT,
            "unified_diff": STRING,
            "diff_truncated": BOOLEAN,
        }
    ),
    "create_project": _result_schema(
        {
            "created": BOOLEAN,
            "path": STRING,
            "revision": STRING,
            "backup_path": {"type": ["string", "null"]},
            "validation": OBJECT,
            "operation_results": ARRAY,
            "project": OBJECT,
        }
    ),
    "edit_project": _result_schema(
        {
            "edited": BOOLEAN,
            "path": STRING,
            "revision": STRING,
            "previous_revision": STRING,
            "backup_path": {"type": ["string", "null"]},
            "validation": OBJECT,
            "operation_results": ARRAY,
            "project": OBJECT,
        }
    ),
    "validate_project": _result_schema(
        {"valid": BOOLEAN, "project": OBJECT, "validator": STRING, "diagnostic": STRING}
    ),
    "render_preview": _result_schema(
        {
            "created": BOOLEAN,
            "path": STRING,
            "frame": INTEGER,
            "size_bytes": INTEGER,
            "managed_output": BOOLEAN,
        }
    ),
    "diagnose_color_workflow": _result_schema(
        {
            "project_path": STRING,
            "compatible": BOOLEAN,
            "project_color_workflow": OBJECT,
            "source_dynamic_ranges": ARRAY,
            "media": ARRAY,
            "issues": ARRAY,
        }
    ),
    "diagnose_missing_media": _result_schema(
        {
            "project_path": STRING,
            "resources": ARRAY,
            "commit_workflow": STRING,
            "visual": OBJECT,
        }
    ),
    "render_preview_batch": _result_schema(
        {
            "requested": INTEGER,
            "created": INTEGER,
            "partial_completion_possible": BOOLEAN,
            "results": ARRAY,
        }
    ),
    "render_contact_sheet": _result_schema(
        {
            "created": BOOLEAN,
            "path": STRING,
            "size_bytes": INTEGER,
            "columns": INTEGER,
            "rows": INTEGER,
            "managed_output": BOOLEAN,
            "cells": ARRAY,
        }
    ),
    "start_render": _result_schema(
        {
            "job_id": STRING,
            "status": STRING,
            "project_path": STRING,
            "output_path": STRING,
        }
    ),
    "render_status": _result_schema(
        {
            "job_id": STRING,
            "status": STRING,
            "progress": {"type": "number"},
            "output_path": STRING,
            "output_size": INTEGER,
            "log_tail": ARRAY,
        }
    ),
    "list_render_jobs": _result_schema(
        {"jobs": ARRAY, "next_cursor": {"type": ["string", "null"]}}
    ),
    "cancel_render": _result_schema(
        {"job_id": STRING, "status": STRING, "cancel_requested": BOOLEAN}
    ),
    "list_project_backups": _result_schema(
        {"project_path": STRING, "backup_count": INTEGER, "backups": ARRAY}
    ),
    "restore_project_backup": _result_schema(
        {
            "restored": BOOLEAN,
            "path": STRING,
            "revision": STRING,
            "previous_revision": STRING,
            "backup_path": STRING,
            "validation": OBJECT,
        }
    ),
}

for tool in TOOLS:
    # Every operation is confined to local/private state. Even when an
    # administrator permits network media reads, these tools never publish or
    # mutate publicly visible internet state.
    tool["annotations"]["openWorldHint"] = False
    described_schema = _clone_schema(tool["inputSchema"])
    _describe_input_schema(described_schema)
    tool["inputSchema"] = described_schema
    tool["outputSchema"] = OUTPUT_SCHEMAS.get(tool["name"], _result_schema())


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
