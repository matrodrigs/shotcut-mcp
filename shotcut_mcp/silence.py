from __future__ import annotations

import math

from .errors import ConflictError, ToolError
from .platform import expand_path, timeline_silences
from .project import (
    MAX_OPERATIONS,
    ProjectDocument,
    check_project_revision,
    preview_edit_operations,
)
from .project_document import _int, _number
from .project_snapshot import project_resources
from .storage import staged_file


def _track_timing(
    document: ProjectDocument,
) -> tuple[dict[str, int], set[str], list[tuple[int, int]]]:
    durations: dict[str, int] = {}
    unlocked: set[str] = set()
    transitions: list[tuple[int, int]] = []
    for track in document.tracks():
        editable = track.properties().get("shotcut:lock") != "1"
        if editable:
            unlocked.add(track.id)
        cursor = 0
        for item in document.sequence(track.playlist):
            end = cursor + document.item_duration(item)
            if editable and document.is_transition(item):
                transitions.append((cursor, end))
            cursor = end
        durations[track.id] = cursor
    merged: list[tuple[int, int]] = []
    for start, end in sorted(transitions):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return durations, unlocked, merged


def plan_silence_cuts(arguments: dict[str, object]) -> dict[str, object]:
    path = expand_path(arguments.get("project_path", ""))
    document = ProjectDocument.load(path)
    if arguments.get("expected_revision") != document.revision:
        raise ConflictError(
            "Inspect the saved project before planning cuts.",
            current_revision=document.revision,
        )
    if any(ref["exists"] is False for ref in project_resources(document)):
        raise ToolError(
            "Resolve missing media before measuring timeline silence.",
            recommended_tool="diagnose_missing_media",
        )
    durations, unlocked, transitions = _track_timing(document)
    timeline_end = max(durations.values(), default=0)
    fps = document.fps
    first = _int(arguments.get("in_frame", 0), "in_frame", 0)
    last = _int(arguments.get("out_frame", timeline_end - 1), "out_frame", first)
    if last >= timeline_end or (last - first + 1) / fps > 600:
        raise ToolError("Select up to 10 minutes within the saved timeline.")
    margin = _number(arguments.get("margin_seconds", 0.15), "margin_seconds", 0)
    with staged_file(path, document.source) as source:
        intervals = timeline_silences(source, first, last, arguments)

    remaining = durations.copy()
    cuts: list[tuple[int, int]] = []
    operations: list[dict[str, object]] = []
    for start, end in reversed(intervals):
        left = max(first, first + math.ceil((start + margin) * fps))
        stop = min(last + 1, first + math.floor((end - margin) * fps))
        if stop <= left or (start <= 0 and end + 1 / 48000 >= (last - first + 1) / fps):
            continue
        while transitions and transitions[-1][0] >= stop:
            transitions.pop()
        if transitions and transitions[-1][1] > left:
            continue
        lengths: dict[int, list[str]] = {}
        for track_id, track_end in durations.items():
            duration = min(track_end, stop) - left
            if track_id in unlocked and duration > 0:
                lengths.setdefault(duration, []).append(track_id)
                remaining[track_id] -= duration
        for duration, ids in lengths.items():
            operations.append(
                {
                    "op": "remove_range",
                    "tracks": ids,
                    "position_frame": left,
                    "duration_frames": duration,
                    "ripple": True,
                }
            )
        if lengths:
            cuts.append((left, max(lengths)))
        if len(operations) > MAX_OPERATIONS:
            raise ToolError(
                "The cuts exceed one edit batch. Choose a shorter analysis range."
            )
    result: dict[str, object] = {
        "project_path": str(path),
        "expected_revision": document.revision,
        "operations": operations,
        "cuts": [
            {"in_frame": at, "duration_frames": length} for at, length in reversed(cuts)
        ],
        "review": None,
    }
    if cuts:
        focus, _ = max(cuts, key=lambda cut: cut[1])
        focus -= sum(length for at, length in cuts if at < focus)
        end_frame = max(remaining.values(), default=0) - 1
        if end_frame < 0:
            raise ToolError("The proposed edit leaves no timeline to review.")
        first_review = max(0, min(focus - round(2 * fps), end_frame))
        last_review = min(end_frame, first_review + max(1, math.floor(4 * fps)) - 1)
        result["review"] = preview_edit_operations(
            {
                "project_path": str(path),
                "expected_revision": document.revision,
                "operations": operations,
            },
            {"in_frame": first_review, "out_frame": last_review},
        )
    else:
        check_project_revision(path, document.revision)
    return result
