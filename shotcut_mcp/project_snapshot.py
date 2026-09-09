"""Read-only projection of an MLT project document for MCP clients."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from .mlt_xml import (
    clock_to_frames,
    properties,
    property_value,
    resource_references,
)
from .path_policy import is_network_resource, resolve_project_resource

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

    from .project_document import ProjectDocument


_SERVICE_KIND_BY_TAG = {
    "producer": "producer",
    "chain": "producer",
    "filter": "filter",
    "transition": "transition",
    "consumer": "consumer",
    "link": "link",
}


FilterSummary = TypedDict(
    "FilterSummary",
    {
        "filter_index": int,
        "filter_id": str | None,
        "service": str | None,
        "shotcut_filter": str | None,
        "enabled": bool,
        "properties": dict[str, str],
    },
)


ExpectedMedia = TypedDict(
    "ExpectedMedia",
    {
        "duration_seconds": float | None,
        "width": str | None,
        "height": str | None,
    },
)


ResourceSummary = TypedDict(
    "ResourceSummary",
    {
        "reference_id": str,
        "owner_id": str | None,
        "owner_tag": str,
        "property": str,
        "resource": str,
        "decoded_resource": str,
        "resolved_path": str | None,
        "exists": bool | None,
        "shotcut_hash": str | None,
        "expected_media": ExpectedMedia,
    },
)


_ItemTiming = TypedDict(
    "_ItemTiming",
    {
        "item_index": int,
        "item_ref": str | None,
        "type": Literal["gap", "transition", "clip"],
        "start_frame": int,
        "duration_frames": int,
        "end_frame": int,
    },
)


# Entry fields are absent for gaps; timing fields are always present.
_ItemSummaryFields = TypedDict(
    "_ItemSummaryFields",
    {
        "producer_id": str | None,
        "in_frame": int,
        "out_frame": int | None,
        "resource": str | None,
        "caption": str | None,
        "filters": list[FilterSummary],
    },
    total=False,
)


class ItemSummary(_ItemTiming, _ItemSummaryFields):
    pass


TrackSummary = TypedDict(
    "TrackSummary",
    {
        "track_id": str,
        "name": str,
        "kind": str,
        "xml_index": int,
        "duration_frames": int,
        "properties": dict[str, str],
        "filters": list[FilterSummary],
        "items": list[ItemSummary],
    },
)


MarkerSummary = TypedDict(
    "MarkerSummary",
    {
        "marker_id": str | None,
        "text": str | None,
        "start_frame": int | None,
        "end_frame": int | None,
        "color": str | None,
    },
)


SubtitleSummary = TypedDict(
    "SubtitleSummary",
    {
        "name": str | None,
        "language": str | None,
        "srt": str | None,
    },
)


LinkSummary = TypedDict(
    "LinkSummary",
    {
        "link_id": str | None,
        "service": str | None,
        "properties": dict[str, str],
    },
)


ColorWorkflowSummary = TypedDict(
    "ColorWorkflowSummary",
    {
        "processing_mode": str,
        "color_transfer": str | None,
        "colorspace": str | None,
        "dynamic_range": Literal["hlg", "pq", "sdr"],
    },
)


# Static contract for the existing MCP projection, without runtime coercion.
ProjectSnapshot = TypedDict(
    "ProjectSnapshot",
    {
        "path": str,
        "revision": str,
        "shotcut_editable": bool,
        "profile": dict[str, str | float],
        "color_workflow": ColorWorkflowSummary,
        "notes": str | None,
        "duration_frames": int,
        "tracks": list[TrackSummary],
        "filters": list[FilterSummary],
        "links": list[LinkSummary],
        "markers": list[MarkerSummary],
        "subtitles": list[SubtitleSummary],
        "resources": list[ResourceSummary],
        "network_resources": list[str],
        "missing_resources": list[str],
        "counts": dict[str, int],
    },
)


@dataclass(frozen=True)
class ProjectRenderInput:
    """Exact project bytes and timing facts captured by one read."""

    source: bytes
    revision: str
    fps: float
    duration_frames: int
    markers: list[MarkerSummary]


def _resource_path(document: ProjectDocument, resource: str) -> Path | None:
    return resolve_project_resource(document.path, document.root.get("root"), resource)


def _filter_summaries(host: Element) -> list[FilterSummary]:
    return [
        {
            "filter_index": index,
            "filter_id": child.get("id"),
            "service": property_value(child, "mlt_service"),
            "shotcut_filter": property_value(child, "shotcut:filter"),
            "enabled": property_value(child, "disable") != "1",
            "properties": properties(child),
        }
        for index, child in enumerate(host.findall("filter"))
    ]


def _expected_media(owner: Element, fps: float) -> ExpectedMedia:
    length = clock_to_frames(property_value(owner, "length"), fps)
    if length is None:
        frame_out = clock_to_frames(owner.get("out"), fps)
        length = frame_out + 1 if frame_out is not None else None
    return {
        "duration_seconds": length / fps if length is not None else None,
        "width": property_value(owner, "meta.media.width")
        or property_value(owner, "meta.media.0.codec.width"),
        "height": property_value(owner, "meta.media.height")
        or property_value(owner, "meta.media.0.codec.height"),
    }


def build_project_snapshot(document: ProjectDocument) -> ProjectSnapshot:
    """Build the stable read-only representation exposed through MCP."""

    resources: list[ResourceSummary] = []
    seen: set[tuple[str | None, str, str]] = set()
    for reference in resource_references(document.root):
        key = (reference.owner_id, reference.name, reference.stored_value)
        if key in seen:
            continue
        seen.add(key)
        path = _resource_path(document, reference.decoded_value)
        resources.append(
            {
                "reference_id": (
                    f"{reference.owner_id or reference.owner_tag}:{reference.name}"
                ),
                "owner_id": reference.owner_id,
                "owner_tag": reference.owner_tag,
                "property": reference.name,
                "resource": reference.stored_value,
                "decoded_resource": reference.decoded_value,
                "resolved_path": str(path) if path else None,
                "exists": path.exists() if path else None,
                "shotcut_hash": property_value(reference.owner, "shotcut:hash"),
                "expected_media": _expected_media(reference.owner, document.fps),
            }
        )
    tracks: list[TrackSummary] = []
    for track in document.tracks():
        cursor = 0
        items: list[ItemSummary] = []
        for index, item in enumerate(document.sequence(track.playlist)):
            duration = document.item_duration(item)
            summary: ItemSummary = {
                "item_index": index,
                "item_ref": (
                    document.item_reference(track, index, item)
                    if item.tag == "entry"
                    else None
                ),
                "type": "gap"
                if item.tag == "blank"
                else "transition"
                if document.is_transition(item)
                else "clip",
                "start_frame": cursor,
                "duration_frames": duration,
                "end_frame": cursor + duration - 1,
            }
            if item.tag == "entry":
                producer_id = item.get("producer")
                producer = document.id_map().get(producer_id or "")
                summary.update(
                    {
                        "producer_id": producer_id,
                        "in_frame": clock_to_frames(item.get("in"), document.fps) or 0,
                        "out_frame": clock_to_frames(item.get("out"), document.fps),
                        "resource": property_value(producer, "resource")
                        if producer is not None
                        else None,
                        "caption": property_value(producer, "shotcut:caption")
                        if producer is not None
                        else None,
                        "filters": _filter_summaries(producer)
                        if producer is not None
                        else [],
                    }
                )
            items.append(summary)
            cursor += duration
        tracks.append(
            {
                "track_id": track.id,
                "name": track.name,
                "kind": track.kind,
                "xml_index": track.xml_index,
                "duration_frames": cursor,
                "properties": properties(track.playlist),
                "filters": _filter_summaries(track.playlist),
                "items": items,
            }
        )
    marker_container = document.markers_container()
    markers: list[MarkerSummary] = []
    if marker_container is not None:
        for marker in marker_container.findall("properties"):
            props = properties(marker)
            markers.append(
                {
                    "marker_id": marker.get("name"),
                    "text": props.get("text"),
                    "start_frame": clock_to_frames(props.get("start"), document.fps),
                    "end_frame": clock_to_frames(props.get("end"), document.fps),
                    "color": props.get("color"),
                }
            )
    main = document.main_tractor()
    subtitles: list[SubtitleSummary] = [
        {
            "name": property_value(child, "feed"),
            "language": property_value(child, "lang"),
            "srt": property_value(child, "text"),
        }
        for child in main.findall("filter")
        if property_value(child, "mlt_service") == "subtitle_feed"
    ]
    profile: dict[str, str | float] = dict(document.profile().attrib)
    profile["fps"] = document.fps
    processing_mode = property_value(main, "shotcut:processingMode") or "Native8Cpu"
    transfer = property_value(main, "shotcut:colorTransfer")
    dynamic_range: Literal["hlg", "pq", "sdr"] = (
        "hlg"
        if transfer == "arib-std-b67"
        else "pq"
        if transfer == "smpte2084"
        else "sdr"
    )
    return {
        "path": str(document.path),
        "revision": document.revision,
        "shotcut_editable": property_value(main, "shotcut") == "1",
        "profile": profile,
        "color_workflow": {
            "processing_mode": processing_mode,
            "color_transfer": transfer,
            "colorspace": document.profile().get("colorspace"),
            "dynamic_range": dynamic_range,
        },
        "notes": property_value(main, "shotcut:projectNote"),
        "duration_frames": max(
            (track["duration_frames"] for track in tracks), default=0
        ),
        "tracks": tracks,
        "filters": _filter_summaries(main),
        "links": [
            {
                "link_id": link.get("id"),
                "service": property_value(link, "mlt_service"),
                "properties": properties(link),
            }
            for link in document.root.findall(".//link")
        ],
        "markers": markers,
        "subtitles": subtitles,
        "resources": resources,
        "network_resources": [
            item["resource"]
            for item in resources
            if is_network_resource(item["decoded_resource"])
        ],
        "missing_resources": [
            resolved_path
            for item in resources
            if item["exists"] is False
            and (resolved_path := item["resolved_path"]) is not None
        ],
        "counts": {
            tag: len(document.root.findall(f".//{tag}"))
            for tag in (
                "producer",
                "chain",
                "playlist",
                "tractor",
                "filter",
                "transition",
                "link",
            )
        },
    }


def project_requirements(document: ProjectDocument) -> dict[str, list[str]]:
    """Project the installed MLT services required by this document."""

    required: dict[str, set[str]] = {
        kind: set() for kind in ("producer", "filter", "transition", "consumer", "link")
    }
    for element in document.root.iter():
        kind = _SERVICE_KIND_BY_TAG.get(element.tag)
        if kind is None:
            continue
        service = property_value(element, "mlt_service")
        if service:
            required[kind].add(service)
    return {kind: sorted(names) for kind, names in required.items()}


def load_project_render_input(path: Path) -> ProjectRenderInput:
    """Capture exact render bytes and timing without a second live-project read."""

    from .project_document import ProjectDocument

    document = ProjectDocument.load(path)
    snapshot = build_project_snapshot(document)
    return ProjectRenderInput(
        source=document.source,
        revision=document.revision,
        fps=document.fps,
        duration_frames=int(snapshot["duration_frames"]),
        markers=list(snapshot["markers"]),
    )
