"""Read-only projection of an MLT project document for MCP clients."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from .mlt_xml import clock_to_frames, properties, property_value
from .path_policy import is_network_resource

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

    from .project_document import ProjectDocument


def _resource_path(document: ProjectDocument, resource: str) -> Path | None:
    if not resource or resource.startswith(("color:", "colour:", "noise:", "tone:")):
        return None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", resource) and not resource.startswith(
        "file://"
    ):
        return None
    if resource.startswith("file://"):
        parsed = urlparse(resource)
        if parsed.netloc:
            cleaned = f"//{parsed.netloc}{unquote(parsed.path)}"
        else:
            cleaned = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", cleaned):
                cleaned = cleaned[1:]
    else:
        cleaned = resource
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return candidate.resolve()
    xml_root = document.root.get("root")
    base = Path(xml_root) if xml_root else document.path.parent
    if not base.is_absolute():
        base = document.path.parent / base
    return (base / candidate).resolve()


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


def build_project_snapshot(document: ProjectDocument) -> dict[str, Any]:
    """Build the stable read-only representation exposed through MCP."""

    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for element in [
        *document.root.findall("producer"),
        *document.root.findall("chain"),
    ]:
        resource = property_value(element, "resource")
        service = property_value(element, "mlt_service")
        if (
            not resource
            or resource in seen
            or service in {"color", "colour", "noise", "tone"}
        ):
            continue
        seen.add(resource)
        path = _resource_path(document, resource)
        resources.append(
            {
                "resource": resource,
                "resolved_path": str(path) if path else None,
                "exists": path.exists() if path else None,
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
    return {
        "path": str(document.path),
        "revision": document.revision,
        "shotcut_editable": property_value(main, "shotcut") == "1",
        "profile": profile,
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
            if is_network_resource(item["resource"])
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
