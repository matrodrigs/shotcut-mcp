"""MCP tool catalog and handlers."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from .errors import ToolError
from .platform import (
    analyze_media_quality,
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
    export_marker_chapters,
    list_backups,
    plan_project_edit,
    render_project_contact_sheet,
    restore_backup,
)
from .protocol import report_progress, schema_errors
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
    "duplicate_item": {
        "required": ["track", "item_index"],
        "optional": ["target_track", "position_frame", "mode"],
        "notes": "Clones the producer/filter chain; on the same track, omission places it after the source.",
    },
    "replace_item_media": {
        "required": ["track", "item_index", "path"],
        "optional": ["caption"],
        "notes": "Preserves source range, placement, filters, and unknown producer properties.",
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
    "move_filter": {
        "required": ["filter_id"],
        "optional": ["before_filter_id"],
        "notes": "Moves before a sibling filter, or after the last sibling when omitted.",
    },
    "remove_filter": {"required": ["filter_id"]},
    "set_notes": {"required": ["notes"]},
    "add_marker": {
        "required": ["start_frame"],
        "optional": ["end_frame", "text", "color"],
    },
    "update_marker": {
        "required": ["marker_id"],
        "optional": ["start_frame", "end_frame", "text", "color"],
        "notes": "Updates marker fields without changing marker identity.",
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
    "delta": {
        "type": "integer",
        "description": "Non-zero signed frame adjustment.",
        "anyOf": [{"maximum": -1}, {"minimum": 1}],
    },
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
    "end_frame": {
        "type": "integer",
        "minimum": 0,
        "description": (
            "Exclusive marker end frame. A value equal to start_frame creates a "
            "point marker; otherwise it must be greater than or equal to start_frame "
            "and the range covers start_frame through end_frame - 1."
        ),
    },
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
        "anyOf": [
            {"minimum": -100, "maximum": -0.05},
            {"minimum": 0.05, "maximum": 100},
        ],
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
    "before_filter_id": {
        "type": "string",
        "description": "Sibling filter id that should follow the moved filter; omit to move last.",
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
    "duplicate_item": {
        "op": "duplicate_item",
        "track": "V1",
        "item_index": 0,
        "position_frame": 120,
        "mode": "insert",
    },
    "replace_item_media": {
        "op": "replace_item_media",
        "track": "V1",
        "item_index": 0,
        "path": "C:/media/replacement.mp4",
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
    "move_filter": {
        "op": "move_filter",
        "filter_id": "filter1",
        "before_filter_id": "filter0",
    },
    "remove_filter": {"op": "remove_filter", "filter_id": "filter0"},
    "set_notes": {"op": "set_notes", "notes": "Rough cut approved"},
    "add_marker": {
        "op": "add_marker",
        "start_frame": 0,
        "text": "Intro",
        "color": "#00A0FF",
    },
    "update_marker": {
        "op": "update_marker",
        "marker_id": "0",
        "text": "Approved intro",
        "end_frame": 90,
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
    if name == "update_marker":
        schema["anyOf"] = [
            {"required": [field]}
            for field in ("start_frame", "end_frame", "text", "color")
        ]
    return {**summary, "schema": schema, "example": OPERATION_EXAMPLES[name]}


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> list[str]:
    """Validate contracts that stay focused outside the compact tools/list schema."""

    if name not in {"edit_project", "plan_project_edit"}:
        return []
    operations = arguments.get("operations")
    if not isinstance(operations, list):
        return []
    errors: list[str] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            continue
        operation_name = operation.get("op")
        if (
            not isinstance(operation_name, str)
            or operation_name not in OPERATION_CATALOG
        ):
            continue
        errors.extend(
            schema_errors(
                operation,
                _operation_details(operation_name)["schema"],
                f"$.operations[{index}]",
            )
        )
    return errors


TRANSACTION_GUARANTEES = [
    "optimistic concurrency using SHA-256 revision",
    "single parse/write for up to 500 operations",
    "MCP lock file",
    "temporary-file MLT validation before replace",
    "atomic replace",
    "timestamped backup retention (20)",
    "unknown XML elements and properties preserved",
]


def capabilities(arguments: dict[str, Any]) -> dict[str, Any]:
    requested = arguments.get("operation")
    if requested is not None and (
        not isinstance(requested, str) or requested not in OPERATION_CATALOG
    ):
        raise ToolError(f"Unknown edit operation: {requested}")
    if isinstance(requested, str):
        return {
            "transaction_guarantees": TRANSACTION_GUARANTEES,
            "operations": {requested: _operation_details(requested)},
        }
    return {
        "compatibility": {
            "shotcut": "26.6.25",
            "mlt": "7.40.x",
            "project_format": "MLT XML",
        },
        "transaction_guarantees": TRANSACTION_GUARANTEES,
        "operations": OPERATION_CATALOG,
        "operation_query": (
            "Pass operation to shotcut_capabilities for its complete schema and example."
        ),
        "render_presets": RENDER_PRESETS,
        "feature_guidance": {
            "quality_analysis": (
                "Use analyze_media_quality for objective source measurements before "
                "proposing cleanup edits; unavailable filters are reported per analyzer."
            ),
            "range_delivery": (
                "start_render accepts either both inclusive in_frame/out_frame values "
                "or one non-empty range marker_id from inspect_project."
            ),
            "chapters": (
                "export_marker_chapters writes Shotcut chapter text from point markers; "
                "range markers and color filtering are opt-in."
            ),
            "edit_primitives": (
                "duplicate_item, replace_item_media, move_filter, and update_marker are "
                "transactional operations and support plan_project_edit."
            ),
            "progress": (
                "Request progress is emitted only when the caller supplies a progress "
                "token; durable render progress continues through render_status."
            ),
        },
        "workflow": [
            "run shotcut_doctor after installing or upgrading Shotcut",
            "use probe_media for stream facts and analyze_media_quality for source QC",
            "inspect_project to obtain revision and current item indexes",
            "optionally list_mlt_services/describe_mlt_service",
            "edit_project with expected_revision and one batch of operations",
            "render_preview or validate_project",
            "optionally export_marker_chapters from point markers",
            "start_render for a full project, inclusive frame range, or range marker; use render_status for durable progress and logs",
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
    report_progress(0, 1, "Validating project with MLT.")
    result = {
        "project": ProjectDocument.load(path).snapshot(),
        **validate_project_file(path, timeout),
    }
    report_progress(1, 1, "Project validation complete.")
    return result


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
    report_progress(0, 1, "Rendering preview frame.")
    result = render_preview(
        expand_path(arguments.get("project_path", "")),
        expand_path(raw_output) if isinstance(raw_output, str) else None,
        frame,
        overwrite,
    )
    report_progress(1, 1, "Preview frame rendered.")
    return result


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


def _operations_input_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 500,
        "items": {
            "type": "object",
            "properties": {"op": {"type": "string", "enum": OP_NAMES}},
            "required": ["op"],
            "additionalProperties": True,
        },
    }


def _revision_guarded_input_schema(
    properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    schema = _object_schema(properties, required)
    schema["anyOf"] = [
        {"required": ["expected_revision"]},
        {
            "required": ["force"],
            "properties": {"force": {"enum": [True]}},
        },
    ]
    return schema


def _edit_project_input_schema() -> dict[str, Any]:
    return _revision_guarded_input_schema(
        {
            "project_path": PATH,
            "expected_revision": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": (
                    "SHA-256 revision returned by inspect_project. Required unless "
                    "force=true was explicitly authorized."
                ),
            },
            "operations": _operations_input_schema(),
            "force": {"type": "boolean", "default": False},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        },
        ["project_path", "operations"],
    )


def _start_render_input_schema() -> dict[str, Any]:
    schema = _object_schema(
        {
            "project_path": PATH,
            "output_path": PATH,
            "preset": {
                "type": "string",
                "enum": list(RENDER_PRESETS),
                "default": "h264-high",
            },
            "consumer_properties": {
                "type": "object",
                "maxProperties": 50,
                "propertyNames": {
                    "type": "string",
                    "pattern": "^[A-Za-z_][A-Za-z0-9_.:-]*$",
                },
                "additionalProperties": {
                    "anyOf": [
                        {"type": "string", "maxLength": 500},
                        {"type": "number"},
                        {"type": "boolean"},
                    ]
                },
            },
            "in_frame": {
                "type": "integer",
                "minimum": 0,
                "description": "Inclusive first project frame; requires out_frame.",
            },
            "out_frame": {
                "type": "integer",
                "minimum": 0,
                "description": "Inclusive last project frame; requires in_frame.",
            },
            "marker_id": {
                "type": "string",
                "description": (
                    "Range marker id from inspect_project; mutually exclusive with "
                    "explicit frames."
                ),
            },
            "overwrite": {"type": "boolean", "default": False},
        },
        ["project_path", "output_path"],
    )
    schema["oneOf"] = [
        {
            "properties": {
                "in_frame": {"enum": [None]},
                "out_frame": {"enum": [None]},
                "marker_id": {"enum": [None]},
            }
        },
        {
            "required": ["in_frame", "out_frame"],
            "properties": {"marker_id": {"enum": [None]}},
        },
        {
            "required": ["marker_id"],
            "properties": {
                "in_frame": {"enum": [None]},
                "out_frame": {"enum": [None]},
            },
        },
    ]
    return schema


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
        "description": (
            "Use before an unfamiliar edit. Omit operation for the catalog, presets, "
            "compatibility, and workflow; pass operation for only its complete schema, "
            "example, and transaction guarantees enforced by plan and edit."
        ),
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
        "name": "analyze_media_quality",
        "title": "Analyze media quality",
        "description": (
            "Use before proposing cleanup or delivery edits. Runs installed FFmpeg "
            "analyzers for silence, black frames, freezes, interlacing, and loudness "
            "and returns bounded structured measurements without changing the media."
        ),
        "inputSchema": _object_schema(
            {
                "path": PATH,
                "analyzers": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {
                        "type": "string",
                        "enum": ["silence", "black", "freeze", "interlace", "loudness"],
                    },
                },
                "start_seconds": {"type": "number", "minimum": 0, "default": 0},
                "duration_seconds": {"type": "number", "minimum": 0.001},
                "audio_stream_index": {"type": "integer", "minimum": 0},
                "video_stream_index": {"type": "integer", "minimum": 0},
                "silence_threshold_db": {
                    "type": "number",
                    "minimum": -120,
                    "maximum": 0,
                    "default": -60,
                },
                "silence_min_duration_seconds": {
                    "type": "number",
                    "minimum": 0.05,
                    "maximum": 3600,
                    "default": 2,
                },
                "black_min_duration_seconds": {
                    "type": "number",
                    "minimum": 0.05,
                    "maximum": 3600,
                    "default": 2,
                },
                "black_pixel_threshold": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.1,
                },
                "black_picture_threshold": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.98,
                },
                "freeze_min_duration_seconds": {
                    "type": "number",
                    "minimum": 0.05,
                    "maximum": 3600,
                    "default": 2,
                },
                "freeze_noise_db": {
                    "type": "number",
                    "minimum": -120,
                    "maximum": 0,
                    "default": -60,
                },
                "dual_mono": {"type": "boolean", "default": False},
                "max_intervals": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 256,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3600,
                    "default": 300,
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
                "expected_revision": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "description": (
                        "Required SHA-256 revision returned by inspect_project; force "
                        "is not supported by plan_project_edit."
                    ),
                },
                "operations": _operations_input_schema(),
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
        "description": (
            "Use to create a new editable Shotcut 26.6 project. If dimensions or frame "
            "rate were not requested, probe representative source media before choosing "
            "the profile; do not treat defaults as user intent. Tracks and clips use the "
            "same shapes as add_track and add_clip."
        ),
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
            "validated atomic write, enforcing each focused operation schema; pass "
            "expected_revision and re-inspect on conflicts instead of using force."
        ),
        "inputSchema": _edit_project_input_schema(),
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
        "description": "Use when the user asks to export. Starts a durable background render for the full project, one inclusive frame range, or one Shotcut range marker; monitor the returned job_id with render_status.",
        "inputSchema": _start_render_input_schema(),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "export_marker_chapters",
        "title": "Export marker chapters",
        "description": (
            "Use to create Shotcut-compatible chapter text from point markers. "
            "Optionally includes range markers or selected marker colors and atomically "
            "protects an existing output."
        ),
        "inputSchema": _object_schema(
            {
                "project_path": PATH,
                "output_path": PATH,
                "expected_revision": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "description": (
                        "Optional SHA-256 revision returned by inspect_project. When "
                        "supplied, export fails if the project has changed."
                    ),
                },
                "include_range_markers": {"type": "boolean", "default": False},
                "colors": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
                },
                "overwrite": {"type": "boolean", "default": False},
            },
            ["project_path", "output_path"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
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
        "description": "Use after list_project_backups and confirmation of the selected backup. Validates and atomically restores it while backing up the current project; pass the current expected_revision unless force=true was explicitly authorized.",
        "inputSchema": _revision_guarded_input_schema(
            {
                "project_path": PATH,
                "backup_path": PATH,
                "expected_revision": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "description": (
                        "SHA-256 revision returned by inspect_project. Required unless "
                        "force=true was explicitly authorized."
                    ),
                },
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
    "expected_revision": "SHA-256 revision returned by inspect_project; the tool schema states whether it is required.",
    "operations": "One or more related edit operations. Query shotcut_capabilities for unfamiliar operation schemas.",
    "timeout_seconds": "Bounded timeout for local MLT processing.",
    "analyzers": "Quality checks to run; defaults to every analyzer supported by this tool.",
    "start_seconds": "Optional media offset in seconds where analysis begins.",
    "duration_seconds": "Optional bounded media duration in seconds to analyze.",
    "audio_stream_index": "Optional global FFprobe stream index for the audio checks.",
    "video_stream_index": "Optional global FFprobe stream index for the video checks.",
    "silence_threshold_db": "Audio level in dB at or below which a segment counts as silence.",
    "silence_min_duration_seconds": "Minimum continuous silence duration to report.",
    "black_min_duration_seconds": "Minimum continuous black-frame duration to report.",
    "black_pixel_threshold": "Per-pixel luminance threshold used by FFmpeg blackdetect.",
    "black_picture_threshold": "Fraction of qualifying dark pixels required for a black frame.",
    "freeze_min_duration_seconds": "Minimum continuous frozen-video duration to report.",
    "freeze_noise_db": "Frame-difference tolerance in dB used by FFmpeg freezedetect.",
    "dual_mono": "Measure mono input as dual-mono when computing EBU R128 loudness.",
    "max_intervals": "Maximum structured intervals retained per analyzer.",
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
    "consumer_properties": "Up to 50 scalar MLT avformat properties. Safe names are allowlisted unless the administrator enables unsafe properties; prefer a named preset.",
    "include_range_markers": "Include Shotcut range markers as chapters; point markers are included by default.",
    "colors": "Optional marker-color allowlist using Shotcut #RRGGBB values.",
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


def _result_schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    *,
    additional_properties: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": additional_properties,
    }
    stable_fields = list(properties or {}) if required is None else required
    if stable_fields:
        result["required"] = stable_fields
    return result


def _output_object(
    properties: dict[str, Any],
    required: list[str],
    *,
    additional_properties: bool = False,
    description: str | None = None,
) -> dict[str, Any]:
    result = _result_schema(
        properties,
        required,
        additional_properties=additional_properties,
    )
    if description:
        result["description"] = description
    return result


def _output_array(items: dict[str, Any], description: str) -> dict[str, Any]:
    return {"type": "array", "items": items, "description": description}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}
NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_INTEGER = {"type": ["integer", "null"]}
NULLABLE_NUMBER = {"type": ["number", "null"]}

PROPERTY_BAG_SCHEMA = {
    "type": "object",
    "description": "Decoded MLT properties keyed by property name.",
    "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
}
FILTER_OUTPUT_SCHEMA = _output_object(
    {
        "filter_index": {
            "type": "integer",
            "minimum": 0,
            "description": "Zero-based filter index on its host.",
        },
        "filter_id": {
            "type": ["string", "null"],
            "description": "Stable MLT filter id, when present.",
        },
        "service": {
            "type": ["string", "null"],
            "description": "Native MLT filter service name.",
        },
        "shotcut_filter": {
            "type": ["string", "null"],
            "description": "Shotcut filter identifier, when present.",
        },
        "enabled": {"type": "boolean", "description": "Whether the filter is enabled."},
        "properties": PROPERTY_BAG_SCHEMA,
    },
    [
        "filter_index",
        "filter_id",
        "service",
        "shotcut_filter",
        "enabled",
        "properties",
    ],
    description="Filter summary returned by inspect_project.",
)
TIMELINE_ITEM_OUTPUT_SCHEMA = _output_object(
    {
        "item_index": {
            "type": "integer",
            "minimum": 0,
            "description": "Zero-based item index within the track.",
        },
        "type": {
            "type": "string",
            "enum": ["clip", "gap", "transition"],
            "description": "Timeline item kind.",
        },
        "start_frame": {
            "type": "integer",
            "minimum": 0,
            "description": "Inclusive zero-based project start frame.",
        },
        "duration_frames": {
            "type": "integer",
            "minimum": 0,
            "description": "Timeline duration in frames.",
        },
        "end_frame": {
            "type": "integer",
            "minimum": -1,
            "description": "Inclusive zero-based project end frame.",
        },
        "producer_id": {
            "type": ["string", "null"],
            "description": "Referenced MLT producer id for an entry.",
        },
        "in_frame": {
            "type": "integer",
            "minimum": 0,
            "description": "Inclusive source in frame for an entry.",
        },
        "out_frame": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Inclusive source out frame for an entry.",
        },
        "resource": {
            "type": ["string", "null"],
            "description": "Stored media resource for an entry.",
        },
        "caption": {
            "type": ["string", "null"],
            "description": "Shotcut clip caption, when present.",
        },
        "filters": _output_array(
            FILTER_OUTPUT_SCHEMA,
            "Clip-local filters in host order.",
        ),
    },
    ["item_index", "type", "start_frame", "duration_frames", "end_frame"],
    description="One gap, transition, or clip entry in a project track.",
)
TRACK_OUTPUT_SCHEMA = _output_object(
    {
        "track_id": {"type": "string", "description": "Stable MLT playlist id."},
        "name": {"type": "string", "description": "Shotcut track name."},
        "kind": {
            "type": "string",
            "enum": ["video", "audio"],
            "description": "Shotcut track kind.",
        },
        "xml_index": {
            "type": "integer",
            "minimum": 0,
            "description": "Zero-based track index in the main tractor.",
        },
        "duration_frames": {
            "type": "integer",
            "minimum": 0,
            "description": "Track duration in project frames.",
        },
        "properties": PROPERTY_BAG_SCHEMA,
        "filters": _output_array(
            FILTER_OUTPUT_SCHEMA,
            "Track-level filters in host order.",
        ),
        "items": _output_array(
            TIMELINE_ITEM_OUTPUT_SCHEMA,
            "Timeline items in playback order.",
        ),
    },
    [
        "track_id",
        "name",
        "kind",
        "xml_index",
        "duration_frames",
        "properties",
        "filters",
        "items",
    ],
    description="Project track and its ordered timeline contents.",
)
MARKER_OUTPUT_SCHEMA = _output_object(
    {
        "marker_id": {
            "type": ["string", "null"],
            "description": "Marker id used by update, remove, chapter, and render tools.",
        },
        "text": {
            "type": ["string", "null"],
            "description": "Marker label.",
        },
        "start_frame": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Inclusive zero-based project start frame.",
        },
        "end_frame": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": (
                "Exclusive zero-based marker end frame. A value equal to start_frame "
                "identifies a point marker."
            ),
        },
        "color": {
            "type": ["string", "null"],
            "description": "Shotcut marker color in #RRGGBB form.",
        },
    },
    ["marker_id", "text", "start_frame", "end_frame", "color"],
    description="Shotcut point or range marker.",
)
SUBTITLE_OUTPUT_SCHEMA = _output_object(
    {
        "name": {"type": ["string", "null"], "description": "Subtitle track name."},
        "language": {
            "type": ["string", "null"],
            "description": "Subtitle language code, when present.",
        },
        "srt": {
            "type": ["string", "null"],
            "description": "Stored SRT subtitle text.",
        },
    },
    ["name", "language", "srt"],
    description="Editable Shotcut subtitle feed.",
)
EXPECTED_MEDIA_OUTPUT_SCHEMA = _output_object(
    {
        "duration_seconds": {
            "type": ["number", "null"],
            "minimum": 0,
            "description": "Expected media duration in seconds.",
        },
        "width": {
            "type": ["string", "null"],
            "description": "Expected media width from MLT metadata.",
        },
        "height": {
            "type": ["string", "null"],
            "description": "Expected media height from MLT metadata.",
        },
    },
    ["duration_seconds", "width", "height"],
    description="Media facts retained in the project for missing-resource matching.",
)
RESOURCE_OUTPUT_SCHEMA = _output_object(
    {
        "reference_id": {
            "type": "string",
            "description": "Stable owner-and-property reference identifier.",
        },
        "owner_id": {
            "type": ["string", "null"],
            "description": "MLT element id that owns the resource.",
        },
        "owner_tag": {"type": "string", "description": "Owning MLT element tag."},
        "property": {"type": "string", "description": "Resource property name."},
        "resource": {
            "type": "string",
            "description": "Resource value stored in the project.",
        },
        "decoded_resource": {
            "type": "string",
            "description": "Decoded resource value before path resolution.",
        },
        "resolved_path": {
            "type": ["string", "null"],
            "description": "Canonical local path when the resource is path-like.",
        },
        "exists": {
            "type": ["boolean", "null"],
            "description": "Whether the resolved local resource exists.",
        },
        "shotcut_hash": {
            "type": ["string", "null"],
            "description": "Shotcut media hash retained for relinking.",
        },
        "expected_media": EXPECTED_MEDIA_OUTPUT_SCHEMA,
    },
    [
        "reference_id",
        "owner_id",
        "owner_tag",
        "property",
        "resource",
        "decoded_resource",
        "resolved_path",
        "exists",
        "shotcut_hash",
        "expected_media",
    ],
    description="One media or data resource referenced by the project.",
)
LINK_OUTPUT_SCHEMA = _output_object(
    {
        "link_id": {
            "type": ["string", "null"],
            "description": "Stable MLT link id, when present.",
        },
        "service": {
            "type": ["string", "null"],
            "description": "Native MLT link service.",
        },
        "properties": PROPERTY_BAG_SCHEMA,
    },
    ["link_id", "service", "properties"],
    description="MLT link summary.",
)
COLOR_WORKFLOW_OUTPUT_SCHEMA = _output_object(
    {
        "processing_mode": {
            "type": "string",
            "description": "Shotcut processing mode.",
        },
        "color_transfer": {
            "type": ["string", "null"],
            "description": "Project transfer characteristic.",
        },
        "colorspace": {
            "type": ["string", "null"],
            "description": "Project MLT colorspace value.",
        },
        "dynamic_range": {
            "type": "string",
            "enum": ["sdr", "hlg", "pq"],
            "description": "Normalized project dynamic range.",
        },
    },
    ["processing_mode", "color_transfer", "colorspace", "dynamic_range"],
    description="Normalized Shotcut project color workflow.",
)
COUNT_NAMES = [
    "producer",
    "chain",
    "playlist",
    "tractor",
    "filter",
    "transition",
    "link",
]
COUNTS_OUTPUT_SCHEMA = _output_object(
    {
        name: {
            "type": "integer",
            "minimum": 0,
            "description": f"Number of {name} elements in the project.",
        }
        for name in COUNT_NAMES
    },
    COUNT_NAMES,
    description="Counts of important MLT element kinds.",
)
PROFILE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "fps": {
            "type": "number",
            "minimum": 0,
            "description": "Computed project frames per second.",
        }
    },
    "required": ["fps"],
    "additionalProperties": True,
    "description": "MLT profile attributes plus computed fps.",
}

PATH_POLICY_OUTPUT_SCHEMA = _output_object(
    {
        "allowed_roots": {
            "type": ["array", "null"],
            "items": STRING,
            "description": "Configured authorized data roots, or null when unrestricted.",
        },
        "require_absolute_paths": BOOLEAN,
        "unsafe_consumer_properties": BOOLEAN,
        "allow_network_resources": BOOLEAN,
    },
    [
        "allowed_roots",
        "require_absolute_paths",
        "unsafe_consumer_properties",
        "allow_network_resources",
    ],
    description="Effective server path and resource policy.",
)
EXECUTABLE_OUTPUT_SCHEMA = _output_object(
    {
        "found": BOOLEAN,
        "path": NULLABLE_STRING,
        "version": NULLABLE_STRING,
        "version_error": NULLABLE_STRING,
    },
    ["found", "path", "version", "version_error"],
    description="Executable discovery and version result.",
)
MELT_OUTPUT_SCHEMA = _output_object(
    {
        **EXECUTABLE_OUTPUT_SCHEMA["properties"],
        "repository_ready": BOOLEAN,
        "repository_error": NULLABLE_STRING,
    },
    [
        "found",
        "path",
        "version",
        "version_error",
        "repository_ready",
        "repository_error",
    ],
    description="Melt discovery, version, and repository readiness.",
)
VALIDATION_OUTPUT_SCHEMA = _output_object(
    {
        "valid": BOOLEAN,
        "return_code": INTEGER,
        "diagnostic": NULLABLE_STRING,
    },
    ["valid", "return_code", "diagnostic"],
    description="Result of processing the project with local Melt.",
)
OPERATION_RESULT_OUTPUT_SCHEMA = _output_object(
    {
        "op": {
            "type": "string",
            "enum": OP_NAMES,
            "description": "Operation that produced this result.",
        }
    },
    ["op"],
    additional_properties=True,
    description="Operation-specific result; identifiers are suitable for later edits.",
)
INITIAL_OPERATION_RESULT_OUTPUT_SCHEMA = _output_object(
    {
        "track_id": STRING,
        "name": STRING,
        "kind": STRING,
        "producer_id": NULLABLE_STRING,
        "duration_frames": INTEGER,
    },
    ["track_id"],
    description="Result of one initial add_track or add_clip operation.",
)
MEDIA_STREAM_OUTPUT_SCHEMA = _output_object(
    {
        "index": NULLABLE_INTEGER,
        "type": NULLABLE_STRING,
        "codec": NULLABLE_STRING,
        "duration_seconds": NULLABLE_NUMBER,
        "width": NULLABLE_INTEGER,
        "height": NULLABLE_INTEGER,
        "pixel_format": NULLABLE_STRING,
        "pixel_bit_depth": NULLABLE_INTEGER,
        "color_primaries": NULLABLE_STRING,
        "color_transfer": NULLABLE_STRING,
        "color_space": NULLABLE_STRING,
        "color_range": NULLABLE_STRING,
        "dynamic_range": NULLABLE_STRING,
        "frame_rate": NULLABLE_NUMBER,
        "sample_rate": NULLABLE_NUMBER,
        "channels": NULLABLE_INTEGER,
        "channel_layout": NULLABLE_STRING,
    },
    ["index", "type", "codec", "duration_seconds"],
    description="Normalized FFprobe stream facts; media-kind fields are optional.",
)
MEDIA_SUMMARY_PROPERTIES = {
    "path": STRING,
    "size_bytes": INTEGER,
    "duration_seconds": NULLABLE_NUMBER,
    "format": NULLABLE_STRING,
    "bit_rate": NULLABLE_NUMBER,
    "streams": _output_array(
        MEDIA_STREAM_OUTPUT_SCHEMA,
        "Normalized audio, video, subtitle, and data streams.",
    ),
    "error": STRING,
}
MEDIA_SUMMARY_OUTPUT_SCHEMA = _output_object(
    MEDIA_SUMMARY_PROPERTIES,
    ["path", "size_bytes", "duration_seconds", "format", "bit_rate", "streams"],
    description="Normalized media summary.",
)
MEDIA_OR_ERROR_OUTPUT_SCHEMA = _output_object(
    MEDIA_SUMMARY_PROPERTIES,
    ["path"],
    description="Normalized media summary, or a bounded probe error.",
)
ISSUE_OUTPUT_SCHEMA = _output_object(
    {"severity": STRING, "code": STRING, "message": STRING},
    ["severity", "code", "message"],
    description="Actionable compatibility finding.",
)
PREVIEW_RESULT_OUTPUT_SCHEMA = _output_object(
    {
        "created": BOOLEAN,
        "path": STRING,
        "frame": INTEGER,
        "size_bytes": INTEGER,
        "managed_output": BOOLEAN,
        "error": STRING,
    },
    ["created", "path", "frame"],
    description="One requested preview result; failure details appear in error.",
)
CONTACT_CELL_OUTPUT_SCHEMA = _output_object(
    {"cell_index": INTEGER, "frame": INTEGER},
    ["cell_index", "frame"],
    description="Contact-sheet cell and its exact project frame.",
)
CHAPTER_OUTPUT_SCHEMA = _output_object(
    {
        "timecode": STRING,
        "frame": INTEGER,
        "text": STRING,
        "marker_id": NULLABLE_STRING,
    },
    ["timecode", "frame", "text", "marker_id"],
    description="One exported Shotcut-compatible chapter.",
)
BACKUP_OUTPUT_SCHEMA = _output_object(
    {
        "path": STRING,
        "size_bytes": INTEGER,
        "modified_at": {"type": "number"},
        "revision": STRING,
    },
    ["path", "size_bytes", "modified_at", "revision"],
    description="Project-owned backup returned by list_project_backups.",
)
RENDER_JOB_SUMMARY_OUTPUT_SCHEMA = _output_object(
    {
        "job_id": NULLABLE_STRING,
        "status": NULLABLE_STRING,
        "project_path": NULLABLE_STRING,
        "output_path": NULLABLE_STRING,
        "preset": NULLABLE_STRING,
        "in_frame": NULLABLE_INTEGER,
        "out_frame": NULLABLE_INTEGER,
        "marker_id": NULLABLE_STRING,
        "marker_text": NULLABLE_STRING,
        "total_frames": NULLABLE_INTEGER,
        "range_duration_frames": NULLABLE_INTEGER,
        "frames_completed": NULLABLE_INTEGER,
        "started_at": NULLABLE_NUMBER,
        "updated_at": NULLABLE_NUMBER,
        "finished_at": NULLABLE_NUMBER,
        "elapsed_seconds": NULLABLE_NUMBER,
        "progress_percent": NULLABLE_NUMBER,
        "current_frame": NULLABLE_INTEGER,
        "return_code": NULLABLE_INTEGER,
        "output_size_bytes": NULLABLE_INTEGER,
        "average_fps": NULLABLE_NUMBER,
        "status_note": NULLABLE_STRING,
    },
    [
        "job_id",
        "status",
        "project_path",
        "output_path",
        "preset",
        "in_frame",
        "out_frame",
        "marker_id",
        "marker_text",
        "total_frames",
        "range_duration_frames",
        "frames_completed",
        "started_at",
        "updated_at",
        "finished_at",
        "elapsed_seconds",
        "progress_percent",
        "current_frame",
        "return_code",
        "output_size_bytes",
        "average_fps",
        "status_note",
    ],
    description="Bounded durable render-job history summary.",
)
HARDWARE_CANDIDATE_OUTPUT_SCHEMA = _output_object(
    {
        "codec": STRING,
        "encoder": STRING,
        "state": STRING,
        "diagnostic": NULLABLE_STRING,
    },
    ["codec", "encoder", "state", "diagnostic"],
    description="One advertised encoder and its smoke-test state.",
)
HARDWARE_SUGGESTION_OUTPUT_SCHEMA = _output_object(
    {
        "verified_hardware": _output_array(
            STRING, "Hardware encoders that passed the smoke test."
        ),
        "recommended": STRING,
        "software_fallback": STRING,
    },
    ["verified_hardware", "recommended", "software_fallback"],
    description="Recommendation for one codec family.",
)
MLT_SERVICE_CHECK_OUTPUT_SCHEMA = _output_object(
    {
        "passed": BOOLEAN,
        "expected": STRING,
        "detected": NULLABLE_STRING,
        "error": NULLABLE_STRING,
        "preferred_service": STRING,
        "services": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "kind": STRING,
                    "name": STRING,
                    "available": BOOLEAN,
                    "metadata": NULLABLE_STRING,
                    "error": STRING,
                },
                "required": ["available"],
                "additionalProperties": False,
            },
        },
        "note": STRING,
    },
    ["passed"],
    description="One compatibility check; fields vary by check kind.",
)
OPERATION_DESCRIPTOR_OUTPUT_SCHEMA = _output_object(
    {
        "required": _output_array(STRING, "Required operation fields."),
        "optional": _output_array(STRING, "Optional operation fields."),
        "notes": STRING,
        "schema": {
            "type": "object",
            "properties": {
                "type": STRING,
                "properties": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": ["string", "array"],
                                "items": STRING,
                            },
                            "description": STRING,
                            "enum": {"type": "array", "items": {}},
                            "minimum": {"type": "number"},
                            "maximum": {"type": "number"},
                            "items": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": True,
                            },
                        },
                        "additionalProperties": True,
                    },
                },
                "required": _output_array(STRING, "Required JSON object properties."),
                "additionalProperties": BOOLEAN,
            },
            "required": ["type", "properties", "required", "additionalProperties"],
            "additionalProperties": True,
        },
        "example": {
            "type": "object",
            "properties": {"op": STRING},
            "required": ["op"],
            "additionalProperties": True,
        },
    },
    ["required"],
    description="Edit-operation summary, with schema and example on focused queries.",
)
ANALYZER_STREAM_OUTPUT_SCHEMA = _output_object(
    {"stream_index": NULLABLE_INTEGER, "status": STRING, "error": STRING},
    ["stream_index", "status"],
    additional_properties=True,
    description="One analyzer result for one media stream.",
)
ANALYZER_OUTPUT_SCHEMA = _output_object(
    {
        "status": STRING,
        "filter": STRING,
        "streams": _output_array(
            ANALYZER_STREAM_OUTPUT_SCHEMA,
            "Per-stream analyzer measurements or failures.",
        ),
        "reason": STRING,
    },
    ["status", "filter", "streams"],
    description="One requested quality analyzer result.",
)
MISSING_CANDIDATE_OUTPUT_SCHEMA = _output_object(
    {
        "candidate_id": STRING,
        "path": STRING,
        "score": INTEGER,
        "match": STRING,
        "verified": BOOLEAN,
        "size_bytes": INTEGER,
        "media": {
            "type": ["object", "null"],
            "properties": MEDIA_SUMMARY_PROPERTIES,
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    [
        "candidate_id",
        "path",
        "score",
        "match",
        "verified",
        "size_bytes",
        "media",
    ],
    description="Ranked replacement candidate.",
)
MISSING_RESOURCE_OUTPUT_SCHEMA = _output_object(
    {
        "reference_id": STRING,
        "missing_path": STRING,
        "stored_resource": STRING,
        "candidates": _output_array(
            MISSING_CANDIDATE_OUTPUT_SCHEMA,
            "Ranked authorized replacement candidates.",
        ),
        "candidate_count": INTEGER,
        "candidates_truncated": BOOLEAN,
    },
    [
        "reference_id",
        "missing_path",
        "stored_resource",
        "candidates",
        "candidate_count",
        "candidates_truncated",
    ],
    description="One missing project resource and its candidates.",
)
PROJECT_RESULT_ITEM_OUTPUT_SCHEMA = _output_object(
    {
        "item_index": INTEGER,
        "type": STRING,
        "start_frame": INTEGER,
        "duration_frames": INTEGER,
    },
    ["item_index", "type", "start_frame", "duration_frames"],
    additional_properties=True,
    description="Compact timeline item identity and timing.",
)
PROJECT_RESULT_TRACK_OUTPUT_SCHEMA = _output_object(
    {
        "track_id": STRING,
        "name": STRING,
        "kind": STRING,
        "items": _output_array(
            PROJECT_RESULT_ITEM_OUTPUT_SCHEMA,
            "Timeline items with stable indexes and timing.",
        ),
    },
    ["track_id", "name", "kind", "items"],
    additional_properties=True,
    description="Compact project track identity and contents.",
)
PROJECT_RESULT_OUTPUT_SCHEMA = _output_object(
    {
        "path": STRING,
        "revision": STRING,
        "duration_frames": INTEGER,
        "tracks": _output_array(
            PROJECT_RESULT_TRACK_OUTPUT_SCHEMA,
            "Tracks carrying identifiers used by later edit operations.",
        ),
        "filters": _output_array(
            FILTER_OUTPUT_SCHEMA,
            "Project-level filters with stable filter identifiers.",
        ),
        "markers": _output_array(
            MARKER_OUTPUT_SCHEMA,
            "Markers with identifiers and exclusive end frames.",
        ),
    },
    ["path", "revision", "duration_frames", "tracks", "filters", "markers"],
    additional_properties=True,
    description="Project snapshot; inspect_project publishes its complete schema.",
)

OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "shotcut_status": _result_schema(
        {
            "ready": BOOLEAN,
            "shotcut": EXECUTABLE_OUTPUT_SCHEMA,
            "melt": MELT_OUTPUT_SCHEMA,
            "ffmpeg": EXECUTABLE_OUTPUT_SCHEMA,
            "ffprobe": EXECUTABLE_OUTPUT_SCHEMA,
            "environment_overrides": {
                "type": "object",
                "additionalProperties": NULLABLE_STRING,
            },
            "path_policy": PATH_POLICY_OUTPUT_SCHEMA,
        }
    ),
    "shotcut_doctor": _result_schema(
        {
            "compatible": BOOLEAN,
            "validated_stack": _output_object(
                {"shotcut": STRING, "mlt": STRING},
                ["shotcut", "mlt"],
                description="Validated Shotcut and MLT compatibility target.",
            ),
            "checks": {
                "type": "object",
                "additionalProperties": MLT_SERVICE_CHECK_OUTPUT_SCHEMA,
            },
            "path_policy": PATH_POLICY_OUTPUT_SCHEMA,
        }
    ),
    "shotcut_capabilities": _result_schema(
        {
            "compatibility": _output_object(
                {"shotcut": STRING, "mlt": STRING, "project_format": STRING},
                ["shotcut", "mlt", "project_format"],
                description="Validated editing stack and project format.",
            ),
            "transaction_guarantees": _output_array(
                STRING,
                "Safety and transaction guarantees that apply to edit operations.",
            ),
            "operations": {
                "type": "object",
                "additionalProperties": OPERATION_DESCRIPTOR_OUTPUT_SCHEMA,
                "description": "Edit operations keyed by op name.",
            },
            "operation_query": STRING,
            "render_presets": {
                "type": "object",
                "additionalProperties": PROPERTY_BAG_SCHEMA,
            },
            "feature_guidance": {
                "type": "object",
                "additionalProperties": STRING,
            },
            "workflow": _output_array(STRING, "Recommended end-to-end workflow."),
        },
        ["transaction_guarantees", "operations"],
    ),
    "probe_media": MEDIA_SUMMARY_OUTPUT_SCHEMA,
    "analyze_media_quality": _result_schema(
        {
            "path": STRING,
            "media_duration_seconds": NULLABLE_NUMBER,
            "start_seconds": {"type": "number"},
            "duration_seconds": NULLABLE_NUMBER,
            "streams": _output_object(
                {
                    "audio_stream_index": NULLABLE_INTEGER,
                    "video_stream_index": NULLABLE_INTEGER,
                },
                ["audio_stream_index", "video_stream_index"],
                description="Selected global FFprobe stream indexes.",
            ),
            "requested_analyzers": _output_array(
                STRING, "Analyzer names requested by the caller."
            ),
            "analyzers": {
                "type": "object",
                "additionalProperties": ANALYZER_OUTPUT_SCHEMA,
            },
        }
    ),
    "inspect_project": _result_schema(
        {
            "path": {
                "type": "string",
                "description": "Authorized project path that was inspected.",
            },
            "revision": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "SHA-256 revision to pass as expected_revision.",
            },
            "shotcut_editable": {
                "type": "boolean",
                "description": "Whether the main tractor is marked as Shotcut-editable.",
            },
            "profile": PROFILE_OUTPUT_SCHEMA,
            "color_workflow": COLOR_WORKFLOW_OUTPUT_SCHEMA,
            "notes": {
                "type": ["string", "null"],
                "description": "Project notes, when present.",
            },
            "duration_frames": {
                "type": "integer",
                "minimum": 0,
                "description": "Project duration in frames.",
            },
            "tracks": _output_array(
                TRACK_OUTPUT_SCHEMA,
                "Project tracks in main-tractor order.",
            ),
            "filters": _output_array(
                FILTER_OUTPUT_SCHEMA,
                "Project-level filters in host order.",
            ),
            "links": _output_array(LINK_OUTPUT_SCHEMA, "MLT links in the project."),
            "markers": _output_array(
                MARKER_OUTPUT_SCHEMA,
                "Shotcut point and range markers.",
            ),
            "subtitles": _output_array(
                SUBTITLE_OUTPUT_SCHEMA,
                "Shotcut subtitle feeds.",
            ),
            "resources": _output_array(
                RESOURCE_OUTPUT_SCHEMA,
                "Media and data resources referenced by the project.",
            ),
            "network_resources": _output_array(
                STRING,
                "Network resource values embedded in the project.",
            ),
            "missing_resources": _output_array(
                STRING,
                "Resolved local resource paths that do not exist.",
            ),
            "counts": COUNTS_OUTPUT_SCHEMA,
        },
        [
            "path",
            "revision",
            "shotcut_editable",
            "profile",
            "color_workflow",
            "notes",
            "duration_frames",
            "tracks",
            "filters",
            "links",
            "markers",
            "subtitles",
            "resources",
            "network_resources",
            "missing_resources",
            "counts",
        ],
        additional_properties=False,
    ),
    "plan_project_edit": _result_schema(
        {
            "planned": BOOLEAN,
            "changed": BOOLEAN,
            "project_path": STRING,
            "base_revision": STRING,
            "prospective_revision": STRING,
            "operation_results": _output_array(
                OPERATION_RESULT_OUTPUT_SCHEMA,
                "Results in the same order as the requested operations.",
            ),
            "validation": VALIDATION_OUTPUT_SCHEMA,
            "project": PROJECT_RESULT_OUTPUT_SCHEMA,
            "unified_diff": STRING,
            "diff_lines": INTEGER,
            "diff_truncated": BOOLEAN,
        }
    ),
    "create_project": _result_schema(
        {
            "created": BOOLEAN,
            "path": STRING,
            "revision": STRING,
            "previous_revision": NULLABLE_STRING,
            "backup_path": NULLABLE_STRING,
            "validation": VALIDATION_OUTPUT_SCHEMA,
            "operation_results": _output_array(
                INITIAL_OPERATION_RESULT_OUTPUT_SCHEMA,
                "Results for initial track and clip operations.",
            ),
            "project": PROJECT_RESULT_OUTPUT_SCHEMA,
        }
    ),
    "edit_project": _result_schema(
        {
            "edited": BOOLEAN,
            "path": STRING,
            "revision": STRING,
            "previous_revision": NULLABLE_STRING,
            "backup_path": NULLABLE_STRING,
            "validation": VALIDATION_OUTPUT_SCHEMA,
            "operation_results": _output_array(
                OPERATION_RESULT_OUTPUT_SCHEMA,
                "Results in the same order as the requested operations.",
            ),
            "project": PROJECT_RESULT_OUTPUT_SCHEMA,
        }
    ),
    "list_mlt_services": _result_schema(
        {
            "kind": STRING,
            "count": INTEGER,
            "services": _output_array(
                STRING, "Installed MLT service names in sorted order."
            ),
        }
    ),
    "describe_mlt_service": _result_schema(
        {
            "kind": STRING,
            "name": STRING,
            "available": BOOLEAN,
            "metadata": NULLABLE_STRING,
        }
    ),
    "validate_project": _result_schema(
        {
            "project": PROJECT_RESULT_OUTPUT_SCHEMA,
            **VALIDATION_OUTPUT_SCHEMA["properties"],
        }
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
            "project_color_workflow": COLOR_WORKFLOW_OUTPUT_SCHEMA,
            "source_dynamic_ranges": _output_array(
                STRING, "Distinct detected source dynamic ranges."
            ),
            "media": _output_array(
                MEDIA_OR_ERROR_OUTPUT_SCHEMA,
                "Bounded source-media summaries or probe errors.",
            ),
            "issues": _output_array(
                ISSUE_OUTPUT_SCHEMA, "Color-workflow compatibility findings."
            ),
            "media_truncated": BOOLEAN,
        }
    ),
    "diagnose_missing_media": _result_schema(
        {
            "project_path": STRING,
            "missing_count": INTEGER,
            "resources": _output_array(
                MISSING_RESOURCE_OUTPUT_SCHEMA,
                "Missing resources and their ranked replacement candidates.",
            ),
            "search": _output_object(
                {
                    "roots": _output_array(STRING, "Authorized roots searched."),
                    "files_examined": INTEGER,
                    "files_limit_reached": BOOLEAN,
                    "hash_bytes_read": INTEGER,
                    "media_probes": INTEGER,
                    "timed_out": BOOLEAN,
                },
                [
                    "roots",
                    "files_examined",
                    "files_limit_reached",
                    "hash_bytes_read",
                    "media_probes",
                    "timed_out",
                ],
                description="Bounded search telemetry.",
            ),
            "commit_workflow": STRING,
            "visual": {
                "type": ["object", "null"],
                "properties": {
                    "created": BOOLEAN,
                    "error": STRING,
                    "path": STRING,
                    "size_bytes": INTEGER,
                    "cells": _output_array(
                        _output_object(
                            {
                                "cell_index": INTEGER,
                                "candidate_id": STRING,
                                "path": STRING,
                            },
                            ["cell_index", "candidate_id", "path"],
                        ),
                        "Visual candidate cells.",
                    ),
                    "skipped": _output_array(
                        _output_object(
                            {"candidate_id": STRING, "reason": STRING},
                            ["candidate_id", "reason"],
                        ),
                        "Candidates that could not produce a frame.",
                    ),
                },
                "required": ["created"],
                "additionalProperties": False,
            },
        }
    ),
    "render_preview_batch": _result_schema(
        {
            "requested": INTEGER,
            "created": INTEGER,
            "partial_completion_possible": BOOLEAN,
            "results": _output_array(
                PREVIEW_RESULT_OUTPUT_SCHEMA,
                "Per-frame successes and bounded failures.",
            ),
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
            "cells": _output_array(
                CONTACT_CELL_OUTPUT_SCHEMA,
                "Contact-sheet cells in display order.",
            ),
        }
    ),
    "detect_hardware_encoders": _result_schema(
        {
            "ffmpeg_path": STRING,
            "platform": STRING,
            "candidates": _output_array(
                HARDWARE_CANDIDATE_OUTPUT_SCHEMA,
                "OS-appropriate hardware encoder smoke tests.",
            ),
            "suggestions": {
                "type": "object",
                "additionalProperties": HARDWARE_SUGGESTION_OUTPUT_SCHEMA,
            },
            "note": STRING,
        }
    ),
    "open_in_shotcut": _result_schema(
        {"opened": BOOLEAN, "path": STRING, "pid": INTEGER}
    ),
    "start_render": _result_schema(
        {
            "job_id": STRING,
            "status": STRING,
            "project_path": STRING,
            "output_path": STRING,
            "preset": STRING,
            "in_frame": NULLABLE_INTEGER,
            "out_frame": NULLABLE_INTEGER,
            "total_frames": NULLABLE_INTEGER,
            "range_duration_frames": NULLABLE_INTEGER,
            "source_duration_frames": NULLABLE_INTEGER,
            "project_revision": NULLABLE_STRING,
            "marker_id": NULLABLE_STRING,
            "marker_text": NULLABLE_STRING,
        }
    ),
    "export_marker_chapters": _result_schema(
        {
            "created": BOOLEAN,
            "path": STRING,
            "project_path": STRING,
            "project_revision": STRING,
            "chapter_count": INTEGER,
            "marker_count": INTEGER,
            "include_range_markers": BOOLEAN,
            "colors": {
                "type": ["array", "null"],
                "items": STRING,
                "description": "Applied marker-color allowlist.",
            },
            "size_bytes": INTEGER,
            "chapters": _output_array(
                CHAPTER_OUTPUT_SCHEMA, "Exported chapters in playback order."
            ),
        }
    ),
    "render_status": _result_schema(
        {
            "job_id": STRING,
            "status": STRING,
            "progress_percent": NULLABLE_NUMBER,
            "frames_completed": NULLABLE_INTEGER,
            "in_frame": NULLABLE_INTEGER,
            "out_frame": NULLABLE_INTEGER,
            "range_duration_frames": NULLABLE_INTEGER,
            "output_path": STRING,
            "output_exists": BOOLEAN,
            "output_size_bytes": NULLABLE_INTEGER,
            "elapsed_seconds": {"type": "number"},
            "eta_seconds": NULLABLE_NUMBER,
            "eta_confidence": NULLABLE_STRING,
            "log_tail": NULLABLE_STRING,
        },
        [
            "job_id",
            "status",
            "progress_percent",
            "in_frame",
            "out_frame",
            "range_duration_frames",
            "output_path",
            "output_exists",
            "output_size_bytes",
            "elapsed_seconds",
            "eta_seconds",
            "eta_confidence",
            "log_tail",
        ],
    ),
    "list_render_jobs": _result_schema(
        {
            "jobs": _output_array(
                RENDER_JOB_SUMMARY_OUTPUT_SCHEMA,
                "Newest-first durable render summaries.",
            ),
            "count": INTEGER,
            "next_cursor": NULLABLE_STRING,
            "status_filter": NULLABLE_STRING,
        }
    ),
    "cancel_render": _result_schema(
        {
            "job_id": STRING,
            "status": STRING,
            "progress_percent": NULLABLE_NUMBER,
            "in_frame": NULLABLE_INTEGER,
            "out_frame": NULLABLE_INTEGER,
            "range_duration_frames": NULLABLE_INTEGER,
            "output_path": STRING,
            "output_exists": BOOLEAN,
            "output_size_bytes": NULLABLE_INTEGER,
            "elapsed_seconds": {"type": "number"},
            "eta_seconds": NULLABLE_NUMBER,
            "eta_confidence": NULLABLE_STRING,
            "log_tail": NULLABLE_STRING,
            "cancellation_requested": BOOLEAN,
        },
        [
            "job_id",
            "status",
            "progress_percent",
            "in_frame",
            "out_frame",
            "range_duration_frames",
            "output_path",
            "output_exists",
            "output_size_bytes",
            "elapsed_seconds",
            "eta_seconds",
            "eta_confidence",
            "log_tail",
        ],
    ),
    "list_project_backups": _result_schema(
        {
            "project_path": STRING,
            "backup_count": INTEGER,
            "backups": _output_array(
                BACKUP_OUTPUT_SCHEMA,
                "Project-owned backups in newest-first order.",
            ),
        }
    ),
    "restore_project_backup": _result_schema(
        {
            "restored": BOOLEAN,
            "path": STRING,
            "revision": STRING,
            "previous_revision": NULLABLE_STRING,
            "backup_path": NULLABLE_STRING,
            "validation": VALIDATION_OUTPUT_SCHEMA,
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
    "analyze_media_quality": lambda arguments: analyze_media_quality(
        expand_path(arguments.get("path", "")), arguments
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
    "export_marker_chapters": export_marker_chapters,
    "render_status": lambda arguments: render_status(arguments.get("job_id", "")),
    "list_render_jobs": list_render_jobs,
    "cancel_render": lambda arguments: cancel_render(arguments.get("job_id", "")),
    "list_project_backups": list_backups_tool,
    "restore_project_backup": restore_backup,
}
