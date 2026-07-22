"""Read-only projection of an MLT project document for MCP clients."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def _resource_path(document: ProjectDocument, resource: str) -> Path | None:
    return resolve_project_resource(document.path, document.root.get("root"), resource)


def _filter_summaries(host: Element) -> list[dict[str, Any]]:
    return [
        {
            "filter_id": child.get("id"),
            "service": property_value(child, "mlt_service"),
            "shotcut_filter": property_value(child, "shotcut:filter"),
            "enabled": property_value(child, "disable") != "1",
            "properties": properties(child),
        }
        for child in host.findall("filter")
    ]


def _expected_media(owner: Element, fps: float) -> dict[str, Any]:
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


def build_project_snapshot(document: ProjectDocument) -> dict[str, Any]:
    """Build the stable read-only representation exposed through MCP."""

    resources: list[dict[str, Any]] = []
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
    tracks: list[dict[str, Any]] = []
    for track in document.tracks():
        cursor = 0
        items: list[dict[str, Any]] = []
        for index, item in enumerate(document.sequence(track.playlist)):
            duration = document.item_duration(item)
            summary: dict[str, Any] = {
                "item_index": index,
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
                    producer_id=producer_id,
                    in_frame=clock_to_frames(item.get("in"), document.fps) or 0,
                    out_frame=clock_to_frames(item.get("out"), document.fps),
                    resource=property_value(producer, "resource")
                    if producer is not None
                    else None,
                    caption=property_value(producer, "shotcut:caption")
                    if producer is not None
                    else None,
                    filters=_filter_summaries(producer) if producer is not None else [],
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
    markers = []
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
    subtitles = [
        {
            "name": property_value(child, "feed"),
            "language": property_value(child, "lang"),
            "srt": property_value(child, "text"),
        }
        for child in main.findall("filter")
        if property_value(child, "mlt_service") == "subtitle_feed"
    ]
    profile: dict[str, Any] = dict(document.profile().attrib)
    profile["fps"] = document.fps
    processing_mode = property_value(main, "shotcut:processingMode") or "Native8Cpu"
    transfer = property_value(main, "shotcut:colorTransfer")
    dynamic_range = (
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
            "colorspace": profile.get("colorspace"),
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
            item["resolved_path"] for item in resources if item["exists"] is False
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
