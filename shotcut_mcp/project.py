"""Transactional, structure-preserving Shotcut MLT XML editing."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from .errors import ConflictError, RequestCancelled, ToolError
from .platform import (
    is_network_resource,
    media_duration,
    probe_media_raw,
    validate_project_file,
)
from .protocol import cancellation_requested
from .storage import (
    is_project_backup,
    list_project_backups,
    project_lock,
    write_project_backup,
)


SEQUENCE_TAGS = {"entry", "blank"}
BACKGROUND_ID = "background"
MAIN_BIN_IDS = {"main_bin", "main bin"}
MAX_OPERATIONS = 500


@dataclass
class EditCandidate:
    path: Path
    document: ProjectDocument
    original: bytes
    original_revision: str
    expected_revision: str | None
    force: bool
    timeout: int
    operation_results: list[dict[str, Any]]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _property(element: ET.Element, name: str) -> str | None:
    for prop in element.findall("property"):
        if prop.get("name") == name:
            return prop.text or ""
    return None


def _set_property(element: ET.Element, name: str, value: Any) -> None:
    for prop in element.findall("property"):
        if prop.get("name") == name:
            prop.text = str(value)
            return
    prop = ET.Element("property", {"name": name})
    prop.text = str(value)
    first_non_property = next(
        (index for index, child in enumerate(element) if child.tag != "property"),
        len(element),
    )
    element.insert(first_non_property, prop)


def _remove_property(element: ET.Element, name: str) -> None:
    for prop in list(element.findall("property")):
        if prop.get("name") == name:
            element.remove(prop)


def _properties(element: ET.Element) -> dict[str, str]:
    return {
        prop.get("name", ""): prop.text or ""
        for prop in element.findall("property")
        if prop.get("name")
    }


def _int(value: Any, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{label} must be an integer.")
    if minimum is not None and value < minimum:
        raise ToolError(f"{label} must be at least {minimum}.")
    return value


def _number(value: Any, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ToolError(f"{label} must be numeric.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{label} must be numeric.") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ToolError(f"{label} must be at least {minimum}.")
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ToolError(f"{label} must be a boolean.")
    return value


def _clock_to_frames(value: str | None, fps: float) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", str(value))
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return round((hours * 3600 + minutes * 60 + seconds) * fps)


def _frames_to_clock(frames: int, fps: float) -> str:
    total_ms = round(frames * 1000 / fps)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _srt(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    previous_end = -1
    for index, item in enumerate(
        sorted(items, key=lambda value: value["start_ms"]), start=1
    ):
        start = _int(item.get("start_ms"), f"items[{index - 1}].start_ms", 0)
        end = _int(item.get("end_ms"), f"items[{index - 1}].end_ms", 1)
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError(f"items[{index - 1}].text must be a non-empty string.")
        if end <= start:
            raise ToolError(f"items[{index - 1}].end_ms must be greater than start_ms.")
        if start < previous_end:
            raise ToolError("Subtitle items on the same track cannot overlap.")
        previous_end = end
        lines.extend([str(index), f"{_srt_time(start)} --> {_srt_time(end)}", text, ""])
    return "\n".join(lines)


@dataclass
class TrackRef:
    xml_index: int
    element: ET.Element
    playlist: ET.Element
    kind: str
    name: str

    @property
    def id(self) -> str:
        return self.playlist.get("id", "")


class ProjectDocument:
    def __init__(self, path: Path, tree: ET.ElementTree, source: bytes) -> None:
        self.path = path
        self.tree = tree
        root = tree.getroot()
        if root is None:
            raise ToolError("The MLT XML project has no root element.")
        self.root: ET.Element = root
        if root.tag != "mlt":
            raise ToolError(f"Unexpected XML root: <{root.tag}>; expected <mlt>.")
        self.source = source
        self.revision = _sha256(source)
        self._id_cache: dict[str, ET.Element] | None = None

    @classmethod
    def load(cls, path: Path) -> "ProjectDocument":
        if not path.is_file():
            raise ToolError(f"Project not found: {path}")
        source = path.read_bytes()
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        try:
            root = ET.fromstring(source, parser=parser)
        except ET.ParseError as exc:
            raise ToolError(f"Invalid MLT XML: {exc}") from exc
        return cls(path, ET.ElementTree(root), source)

    @classmethod
    def new(
        cls,
        path: Path,
        *,
        width: int,
        height: int,
        fps_num: int,
        fps_den: int,
        title: str,
    ) -> "ProjectDocument":
        divisor = math.gcd(width, height)
        root = ET.Element(
            "mlt",
            {
                "LC_NUMERIC": "C",
                "version": "7.40.0",
                "title": "Shotcut version 26.6.25",
                "producer": "tractor0",
                "root": str(path.parent).replace("\\", "/"),
            },
        )
        ET.SubElement(
            root,
            "profile",
            {
                "description": f"{width}x{height} {fps_num / fps_den:.3f} fps",
                "width": str(width),
                "height": str(height),
                "progressive": "1",
                "sample_aspect_num": "1",
                "sample_aspect_den": "1",
                "display_aspect_num": str(width // divisor),
                "display_aspect_den": str(height // divisor),
                "frame_rate_num": str(fps_num),
                "frame_rate_den": str(fps_den),
                "colorspace": "709" if height >= 720 else "601",
            },
        )
        background_producer = ET.SubElement(
            root, "producer", {"id": "black", "in": "0", "out": "0"}
        )
        for name, value in (
            ("length", "1"),
            ("eof", "pause"),
            ("resource", "0"),
            ("mlt_service", "color"),
            ("mlt_image_format", "rgba"),
            ("set.test_audio", "0"),
        ):
            _set_property(background_producer, name, value)
        background = ET.SubElement(root, "playlist", {"id": BACKGROUND_ID})
        ET.SubElement(background, "entry", {"producer": "black", "in": "0", "out": "0"})
        video = ET.SubElement(root, "playlist", {"id": "playlist_v1"})
        _set_property(video, "shotcut:video", 1)
        _set_property(video, "shotcut:name", "V1")
        ET.SubElement(video, "blank", {"length": "1"})
        main = ET.SubElement(root, "tractor", {"id": "tractor0", "in": "0", "out": "0"})
        _set_property(main, "shotcut", 1)
        _set_property(main, "shotcut:projectAudioChannels", 2)
        _set_property(main, "shotcut:processingMode", "native-8bit")
        _set_property(main, "shotcut:projectNote", title)
        ET.SubElement(main, "track", {"producer": BACKGROUND_ID})
        ET.SubElement(main, "track", {"producer": "playlist_v1"})
        document = cls(path, ET.ElementTree(root), b"")
        document.ensure_default_track_transitions()
        document.source = document.to_bytes()
        document.revision = _sha256(document.source)
        return document

    def invalidate(self) -> None:
        self._id_cache = None

    def id_map(self) -> dict[str, ET.Element]:
        if self._id_cache is None:
            mapping: dict[str, ET.Element] = {}
            for element in self.root.iter():
                element_id = element.get("id")
                if not element_id:
                    continue
                if element_id in mapping:
                    raise ToolError(f"Duplicate XML id: {element_id}")
                mapping[element_id] = element
            self._id_cache = mapping
        return self._id_cache

    def profile(self) -> ET.Element:
        profile = self.root.find("profile")
        if profile is None:
            raise ToolError("The project does not contain an MLT profile.")
        return profile

    @property
    def fps(self) -> float:
        profile = self.profile()
        numerator = int(profile.get("frame_rate_num", "25"))
        denominator = int(profile.get("frame_rate_den", "1"))
        return numerator / denominator if denominator else 25.0

    def main_tractor(self) -> ET.Element:
        producer_id = self.root.get("producer")
        candidate = self.id_map().get(producer_id or "")
        if candidate is not None and candidate.tag == "tractor":
            return candidate
        tractors = self.root.findall("tractor")
        shotcut = [
            tractor for tractor in tractors if _property(tractor, "shotcut") == "1"
        ]
        if shotcut:
            if len(shotcut) == 1:
                return shotcut[0]
            raise ToolError(
                "The project contains multiple tractors marked as the Shotcut timeline."
            )
        if len(tractors) == 1:
            return tractors[0]
        if tractors:
            raise ToolError(
                "The project contains multiple tractors and does not identify the main one."
            )
        raise ToolError("The project does not contain a timeline tractor.")

    def track_container(self) -> ET.Element:
        tractor = self.main_tractor()
        multitrack = tractor.find("multitrack")
        return multitrack if multitrack is not None else tractor

    def tracks(self, include_special: bool = False) -> list[TrackRef]:
        refs: list[TrackRef] = []
        for xml_index, track in enumerate(self.track_container().findall("track")):
            playlist = self.id_map().get(track.get("producer", ""))
            if playlist is None or playlist.tag != "playlist":
                continue
            playlist_id = playlist.get("id", "")
            if not include_special and playlist_id in {BACKGROUND_ID, *MAIN_BIN_IDS}:
                continue
            props = _properties(playlist)
            kind = "audio" if props.get("shotcut:audio") == "1" else "video"
            if props.get("shotcut:video") != "1" and props.get("shotcut:audio") != "1":
                kind = "unknown"
            refs.append(
                TrackRef(
                    xml_index=xml_index,
                    element=track,
                    playlist=playlist,
                    kind=kind,
                    name=props.get("shotcut:name")
                    or playlist_id
                    or f"Track {xml_index}",
                )
            )
        return refs

    def find_track(self, selector: Any) -> TrackRef:
        if not isinstance(selector, str) or not selector.strip():
            raise ToolError("track must be a track name or id.")
        matches = [
            track for track in self.tracks() if selector in {track.id, track.name}
        ]
        if len(matches) != 1:
            raise ToolError(
                f"Track {selector!r} was not found uniquely. Options: "
                + ", ".join(f"{track.name} ({track.id})" for track in self.tracks())
            )
        return matches[0]

    def new_id(self, prefix: str) -> str:
        existing = self.id_map()
        for _ in range(10000):
            candidate = f"{prefix}_{uuid.uuid4().hex[:12]}"
            if candidate not in existing:
                return candidate
        raise ToolError("Could not generate a unique XML id.")

    def clone_service(self, original: ET.Element) -> ET.Element:
        clone = copy.deepcopy(original)
        existing = set(self.id_map())
        replacements: dict[str, str] = {}
        for element in clone.iter():
            old_id = element.get("id")
            if not old_id:
                continue
            prefix = re.sub(r"[^A-Za-z0-9_]+", "_", element.tag) or "service"
            for _ in range(10000):
                candidate = f"{prefix}_{uuid.uuid4().hex[:12]}"
                if candidate not in existing:
                    break
            else:
                raise ToolError("Could not clone the service with unique XML ids.")
            replacements[old_id] = candidate
            existing.add(candidate)
            element.set("id", candidate)
        for element in clone.iter():
            for name, value in list(element.attrib.items()):
                if value in replacements:
                    element.set(name, replacements[value])
        return clone

    def isolate_entry_service(self, entry: ET.Element) -> ET.Element:
        service_id = entry.get("producer", "")
        original = self.id_map().get(service_id)
        if original is None:
            raise ToolError("The clip producer was not found.")
        references = sum(
            1 for element in self.root.iter() if element.get("producer") == service_id
        )
        if references <= 1:
            return original
        clone = self.clone_service(original)
        self.insert_root_before_main(clone)
        entry.set("producer", clone.get("id", ""))
        return clone

    def insert_root_before_main(self, element: ET.Element) -> None:
        main = self.main_tractor()
        children = list(self.root)
        index = children.index(main)
        if element.tag in {"producer", "chain", "tractor"}:
            # Shotcut's UI resolves timeline entries while reading each playlist.
            # Keep services referenced by editable tracks before those playlists;
            # MLT itself accepts forward references, but Shotcut drops them when
            # it converts the XML into its timeline model.
            for track in self.track_container().findall("track"):
                playlist = self.id_map().get(track.get("producer", ""))
                if playlist is not None and playlist.tag == "playlist":
                    try:
                        index = min(index, children.index(playlist))
                    except ValueError:
                        pass
        self.root.insert(index, element)
        self.invalidate()

    def normalize_root_service_order(self) -> None:
        """Place existing timeline services before editable track playlists."""
        main = self.main_tractor()
        editable_playlists = [track.playlist for track in self.tracks()]
        children = list(self.root)
        anchors = [
            playlist
            for playlist in editable_playlists
            if playlist in children
        ]
        if not anchors:
            return
        anchor = min(anchors, key=children.index)
        services = [
            child
            for child in children
            if child is not main
            and child.tag in {"producer", "chain", "tractor"}
            and child.get("id") != "black"
            and children.index(child) > children.index(anchor)
        ]
        for service in services:
            self.root.remove(service)
            self.root.insert(list(self.root).index(anchor), service)
        if services:
            self.invalidate()

    def item_duration(self, element: ET.Element) -> int:
        if element.tag == "blank":
            length = _clock_to_frames(element.get("length"), self.fps)
            return max(0, length or 0)
        if element.tag != "entry":
            return 0
        frame_in = _clock_to_frames(element.get("in"), self.fps) or 0
        frame_out = _clock_to_frames(element.get("out"), self.fps)
        if frame_out is not None:
            return max(0, frame_out - frame_in + 1)
        producer = self.id_map().get(element.get("producer", ""))
        if producer is None:
            return 0
        frame_out = _clock_to_frames(producer.get("out"), self.fps)
        length = _clock_to_frames(_property(producer, "length"), self.fps)
        return max(
            0, (frame_out - frame_in + 1) if frame_out is not None else (length or 0)
        )

    def sequence(self, playlist: ET.Element) -> list[ET.Element]:
        return [child for child in playlist if child.tag in SEQUENCE_TAGS]

    def replace_sequence(
        self, playlist: ET.Element, sequence: list[ET.Element]
    ) -> None:
        indices = [
            index for index, child in enumerate(playlist) if child.tag in SEQUENCE_TAGS
        ]
        insertion_index = indices[0] if indices else len(playlist)
        for child in list(playlist):
            if child.tag in SEQUENCE_TAGS:
                playlist.remove(child)
        for offset, child in enumerate(sequence):
            playlist.insert(insertion_index + offset, child)

    def remove_unreferenced_services(self, service_ids: set[str]) -> None:
        removed = False
        for service_id in service_ids:
            if not service_id or self.root.get("producer") == service_id:
                continue
            if any(
                element.get("producer") == service_id for element in self.root.iter()
            ):
                continue
            service = self.id_map().get(service_id)
            if service is None or service not in list(self.root):
                continue
            if service.tag not in {"producer", "chain", "tractor"}:
                continue
            self.root.remove(service)
            removed = True
        if removed:
            self.invalidate()

    def consolidate_blanks(self, sequence: list[ET.Element]) -> list[ET.Element]:
        result: list[ET.Element] = []
        for item in sequence:
            duration = self.item_duration(item)
            if duration <= 0:
                continue
            if item.tag == "blank" and result and result[-1].tag == "blank":
                result[-1].set("length", str(self.item_duration(result[-1]) + duration))
            else:
                result.append(item)
        return result or [ET.Element("blank", {"length": "1"})]

    def is_transition(self, entry: ET.Element) -> bool:
        if entry.tag != "entry":
            return False
        producer = self.id_map().get(entry.get("producer", ""))
        return (
            producer is not None
            and producer.tag == "tractor"
            and _property(producer, "shotcut:transition") not in (None, "", "0")
        )

    def split_sequence_at(self, sequence: list[ET.Element], frame: int) -> int:
        if frame < 0:
            raise ToolError("The position must be zero or positive.")
        cursor = 0
        for index, item in enumerate(sequence):
            duration = self.item_duration(item)
            if frame == cursor:
                return index
            if cursor < frame < cursor + duration:
                if self.is_transition(item):
                    raise ToolError("Cannot split inside a transition.")
                offset = frame - cursor
                left = copy.deepcopy(item)
                right = copy.deepcopy(item)
                if item.tag == "blank":
                    left.set("length", str(offset))
                    right.set("length", str(duration - offset))
                else:
                    source_in = _clock_to_frames(item.get("in"), self.fps) or 0
                    left.set("in", str(source_in))
                    left.set("out", str(source_in + offset - 1))
                    right.set("in", str(source_in + offset))
                    right.set("out", str(source_in + duration - 1))
                sequence[index : index + 1] = [left, right]
                return index + 1
            cursor += duration
        if frame == cursor:
            return len(sequence)
        sequence.append(ET.Element("blank", {"length": str(frame - cursor)}))
        return len(sequence)

    def place_item(
        self, playlist: ET.Element, item: ET.Element, position: int | None, mode: str
    ) -> None:
        sequence = self.sequence(playlist)
        if (
            position is None
            and len(sequence) == 1
            and sequence[0].tag == "blank"
            and self.item_duration(sequence[0]) == 1
        ):
            # Shotcut represents a new/emptied track with one technical blank frame.
            # Treat that sentinel as an empty timeline when appending the first item.
            sequence = []
        total = sum(self.item_duration(node) for node in sequence)
        frame = total if position is None else position
        if mode not in {"insert", "overwrite"}:
            raise ToolError("mode must be insert or overwrite.")
        start_index = self.split_sequence_at(sequence, frame)
        if mode == "insert":
            sequence.insert(start_index, item)
        else:
            end_index = self.split_sequence_at(
                sequence, frame + self.item_duration(item)
            )
            removed_service_ids = {
                node.get("producer", "")
                for node in sequence[start_index:end_index]
                if node.tag == "entry"
            }
            sequence[start_index:end_index] = [item]
        self.replace_sequence(playlist, self.consolidate_blanks(sequence))
        if mode == "overwrite":
            self.remove_unreferenced_services(removed_service_ids)
        self.update_main_duration()

    def update_main_duration(self) -> None:
        duration = max(
            (
                sum(self.item_duration(item) for item in self.sequence(track.playlist))
                for track in self.tracks()
            ),
            default=1,
        )
        main = self.main_tractor()
        main.set("in", "0")
        main.set("out", str(max(0, duration - 1)))
        background = self.id_map().get(BACKGROUND_ID)
        black = self.id_map().get("black")
        if background is not None and black is not None:
            black.set("out", str(max(0, duration - 1)))
            _set_property(black, "length", duration)
            entries = self.sequence(background)
            if entries:
                entries[0].set("in", "0")
                entries[0].set("out", str(max(0, duration - 1)))

    def ensure_shotcut_structure(self) -> None:
        """Upgrade minimal/legacy MLT timelines to Shotcut's expected background layout."""
        main = self.main_tractor()
        _set_property(main, "shotcut", 1)
        self.normalize_root_service_order()
        if _property(main, "shotcut:projectNote") is None:
            legacy_notes = _property(main, "shotcut:projectNotes")
            if legacy_notes is not None:
                _set_property(main, "shotcut:projectNote", legacy_notes)
        container = self.track_container()
        existing_tracks = container.findall("track")
        if existing_tracks:
            first = self.id_map().get(existing_tracks[0].get("producer", ""))
            if first is not None and first.get("id") == BACKGROUND_ID:
                return
            background_index = next(
                (
                    index
                    for index, item in enumerate(existing_tracks)
                    if item.get("producer") == BACKGROUND_ID
                ),
                None,
            )
            if background_index is not None:
                background_track = existing_tracks[background_index]
                old_indices = {
                    id(element): index for index, element in enumerate(existing_tracks)
                }
                container.remove(background_track)
                first_track_position = next(
                    (
                        index
                        for index, child in enumerate(container)
                        if child.tag == "track"
                    ),
                    len(container),
                )
                container.insert(first_track_position, background_track)
                new_elements = container.findall("track")
                self.remap_track_transitions(
                    {
                        old_indices[id(element)]: index
                        for index, element in enumerate(new_elements)
                    }
                )
                return
        duration = max(
            (
                sum(self.item_duration(item) for item in self.sequence(track.playlist))
                for track in self.tracks()
            ),
            default=1,
        )
        black_id = "black" if "black" not in self.id_map() else self.new_id("black")
        background_id = (
            BACKGROUND_ID
            if BACKGROUND_ID not in self.id_map()
            else self.new_id("background")
        )
        black = ET.Element(
            "producer", {"id": black_id, "in": "0", "out": str(duration - 1)}
        )
        for name, value in (
            ("length", duration),
            ("eof", "pause"),
            ("resource", "0"),
            ("mlt_service", "color"),
            ("mlt_image_format", "rgba"),
            ("set.test_audio", 0),
        ):
            _set_property(black, name, value)
        background = ET.Element("playlist", {"id": background_id})
        ET.SubElement(
            background,
            "entry",
            {"producer": black_id, "in": "0", "out": str(duration - 1)},
        )
        self.insert_root_before_main(black)
        self.insert_root_before_main(background)
        old_count = len(existing_tracks)
        insertion_index = next(
            (index for index, child in enumerate(container) if child.tag == "track"),
            len(container),
        )
        container.insert(
            insertion_index, ET.Element("track", {"producer": background_id})
        )
        self.remap_track_transitions({old: old + 1 for old in range(old_count)})
        self.invalidate()
        self.ensure_default_track_transitions()

    def _transition(
        self, service: str, a_track: int, b_track: int, **props: Any
    ) -> ET.Element:
        transition = ET.Element("transition", {"id": self.new_id("transition")})
        _set_property(transition, "a_track", a_track)
        _set_property(transition, "b_track", b_track)
        for key, value in props.items():
            _set_property(transition, key, value)
        _set_property(transition, "mlt_service", service)
        return transition

    def ensure_default_track_transitions(self) -> None:
        main = self.main_tractor()
        tracks = self.tracks()
        existing = [child for child in main if child.tag == "transition"]
        bottom_video = next(
            (track.xml_index for track in tracks if track.kind == "video"), 0
        )
        for track in tracks:
            has_mix = any(
                _property(item, "mlt_service") == "mix"
                and _property(item, "b_track") == str(track.xml_index)
                and _property(item, "always_active") == "1"
                for item in existing
            )
            if not has_mix:
                mix = self._transition(
                    "mix", 0, track.xml_index, always_active=1, sum=1
                )
                _set_property(mix, "shotcut:mcpDefault", 1)
                main.append(mix)
                existing.append(mix)
            if track.kind == "video":
                has_composite = any(
                    _property(item, "mlt_service")
                    in {"qtblend", "movit.overlay", "frei0r.cairoblend"}
                    and _property(item, "b_track") == str(track.xml_index)
                    for item in existing
                )
                if not has_composite:
                    a_track = 0 if track.xml_index == bottom_video else bottom_video
                    composite = self._transition(
                        "qtblend", a_track, track.xml_index, threads=0
                    )
                    _set_property(
                        composite,
                        "disable",
                        1 if track.xml_index == bottom_video else 0,
                    )
                    _set_property(composite, "shotcut:mcpDefault", 1)
                    main.append(composite)
                    existing.append(composite)

    def remap_track_transitions(self, mapping: Mapping[int, int | None]) -> None:
        main = self.main_tractor()
        for transition in list(main.findall("transition")):
            a = _property(transition, "a_track")
            b = _property(transition, "b_track")
            if a is None or b is None:
                continue
            old_a, old_b = int(a), int(b)
            new_a, new_b = mapping.get(old_a, old_a), mapping.get(old_b, old_b)
            if new_a is None or new_b is None:
                main.remove(transition)
                continue
            _set_property(transition, "a_track", new_a)
            _set_property(transition, "b_track", new_b)

    def add_track(self, operation: dict[str, Any]) -> dict[str, Any]:
        kind = operation.get("kind")
        if kind not in {"video", "audio"}:
            raise ToolError("kind must be video or audio.")
        current = self.tracks()
        number = 1 + sum(track.kind == kind for track in current)
        name = operation.get("name") or ("V" if kind == "video" else "A") + str(number)
        if not isinstance(name, str) or not name.strip():
            raise ToolError("name must be a non-empty string.")
        if any(track.name == name for track in current):
            raise ToolError(f"A track named {name!r} already exists.")
        playlist_id = self.new_id("playlist")
        playlist = ET.Element("playlist", {"id": playlist_id})
        _set_property(playlist, f"shotcut:{kind}", 1)
        _set_property(playlist, "shotcut:name", name)
        if kind == "audio":
            _set_property(playlist, "hide", 1)
        ET.SubElement(playlist, "blank", {"length": "1"})
        self.insert_root_before_main(playlist)

        container = self.track_container()
        track_elements = container.findall("track")
        if kind == "video":
            insertion_index = next(
                (
                    list(container).index(track.element)
                    for track in current
                    if track.kind == "audio"
                ),
                max(
                    (list(container).index(item) for item in track_elements), default=-1
                )
                + 1,
            )
        else:
            insertion_index = (
                max(
                    (list(container).index(item) for item in track_elements), default=-1
                )
                + 1
            )
        old_count = len(track_elements)
        new_xml_index = sum(
            1 for child in list(container)[:insertion_index] if child.tag == "track"
        )
        container.insert(
            insertion_index, ET.Element("track", {"producer": playlist_id})
        )
        mapping = {
            old: (old + 1 if old >= new_xml_index else old) for old in range(old_count)
        }
        self.remap_track_transitions(mapping)
        self.invalidate()
        self.ensure_default_track_transitions()
        return {"track_id": playlist_id, "name": name, "kind": kind}

    def remove_track(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        removed_service_ids = {
            item.get("producer", "")
            for item in self.sequence(track.playlist)
            if item.tag == "entry"
        }
        container = self.track_container()
        old_count = len(container.findall("track"))
        container.remove(track.element)
        mapping: dict[int, int | None] = {
            old: (
                None
                if old == track.xml_index
                else old - 1
                if old > track.xml_index
                else old
            )
            for old in range(old_count)
        }
        self.remap_track_transitions(mapping)
        if not any(
            item.get("producer") == track.id for item in container.findall("track")
        ):
            self.root.remove(track.playlist)
        self.invalidate()
        self.remove_unreferenced_services(removed_service_ids)
        self.ensure_default_track_transitions()
        self.update_main_duration()
        return {"removed_track": track.name, "track_id": track.id}

    def update_track(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        if "name" in operation:
            name = operation["name"]
            if not isinstance(name, str) or not name.strip():
                raise ToolError("name must be a non-empty string.")
            if any(item is not track and item.name == name for item in self.tracks()):
                raise ToolError(f"A track named {name!r} already exists.")
            _set_property(track.playlist, "shotcut:name", name)
            track.name = name
        if "locked" in operation:
            value = operation["locked"]
            if not isinstance(value, bool):
                raise ToolError("locked must be a boolean.")
            _set_property(track.playlist, "shotcut:lock", 1 if value else 0)
        hide = int(_property(track.playlist, "hide") or 0)
        if "hidden" in operation:
            value = operation["hidden"]
            if not isinstance(value, bool):
                raise ToolError("hidden must be a boolean.")
            hide = (hide | 1) if value else (hide & ~1)
        if "muted" in operation:
            value = operation["muted"]
            if not isinstance(value, bool):
                raise ToolError("muted must be a boolean.")
            hide = (hide | 2) if value else (hide & ~2)
        _set_property(track.playlist, "hide", hide)
        if "composite" in operation:
            composite = operation["composite"]
            if not isinstance(composite, bool) or track.kind != "video":
                raise ToolError(
                    "composite must be a boolean and applies only to video tracks."
                )
            transition = next(
                (
                    item
                    for item in self.main_tractor().findall("transition")
                    if _property(item, "b_track") == str(track.xml_index)
                    and _property(item, "mlt_service")
                    in {"qtblend", "movit.overlay", "frei0r.cairoblend"}
                ),
                None,
            )
            if transition is None:
                self.ensure_default_track_transitions()
                transition = next(
                    item
                    for item in self.main_tractor().findall("transition")
                    if _property(item, "b_track") == str(track.xml_index)
                    and _property(item, "mlt_service") == "qtblend"
                )
            _set_property(transition, "disable", 0 if composite else 1)
        return {"track_id": track.id, "name": track.name, "updated": True}

    def move_track(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        before = self.find_track(operation.get("before"))
        if track.id == before.id:
            return {"moved": False, "track_id": track.id}
        container = self.track_container()
        old_elements = container.findall("track")
        old_indices = {id(element): index for index, element in enumerate(old_elements)}
        container.remove(track.element)
        insertion_index = list(container).index(before.element)
        container.insert(insertion_index, track.element)
        new_elements = container.findall("track")
        mapping = {
            old_indices[id(element)]: index
            for index, element in enumerate(new_elements)
        }
        self.remap_track_transitions(mapping)
        self.invalidate()
        self.ensure_default_track_transitions()
        return {"moved": True, "track_id": track.id, "before": before.id}

    def create_media_producer(
        self, operation: dict[str, Any]
    ) -> tuple[ET.Element, ET.Element]:
        raw_path = operation.get("path")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string.")
        media_path = Path(os.path.expandvars(raw_path)).expanduser().resolve()
        if not media_path.is_file():
            raise ToolError(f"Media not found: {media_path}")
        probe = probe_media_raw(media_path)
        duration_seconds = media_duration(probe)
        image_duration = _number(
            operation.get("image_duration_seconds", 5.0), "image_duration_seconds", 0.04
        )
        full_frames = max(1, math.ceil((duration_seconds or image_duration) * self.fps))
        if "in_frame" in operation:
            frame_in = _int(operation["in_frame"], "in_frame", 0)
        else:
            frame_in = round(
                _number(operation.get("in_seconds", 0), "in_seconds", 0) * self.fps
            )
        if "out_frame" in operation:
            frame_out = _int(operation["out_frame"], "out_frame", 0)
        else:
            out_seconds = _number(
                operation.get("out_seconds", duration_seconds or image_duration),
                "out_seconds",
                0.001,
            )
            frame_out = math.ceil(out_seconds * self.fps) - 1
        if frame_out < frame_in or frame_out >= full_frames:
            raise ToolError(
                f"Invalid range: in={frame_in}, out={frame_out}, media={full_frames} frames."
            )
        producer_id = self.new_id("producer")
        producer = ET.Element(
            "producer", {"id": producer_id, "in": "0", "out": str(full_frames - 1)}
        )
        for name, value in (
            ("length", full_frames),
            ("eof", "pause"),
            ("resource", str(media_path).replace("\\", "/")),
            ("mlt_service", "avformat-novalidate"),
            ("seekable", 1),
            ("shotcut:skipConvert", 1),
            ("shotcut:caption", operation.get("caption") or media_path.name),
        ):
            _set_property(producer, name, value)
        entry = ET.Element(
            "entry",
            {"producer": producer_id, "in": str(frame_in), "out": str(frame_out)},
        )
        self.insert_root_before_main(producer)
        return producer, entry

    def add_clip(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        producer, entry = self.create_media_producer(operation)
        position = operation.get("position_frame")
        if position is not None:
            position = _int(position, "position_frame", 0)
        self.place_item(
            track.playlist, entry, position, operation.get("mode", "insert")
        )
        return {
            "producer_id": producer.get("id"),
            "track_id": track.id,
            "duration_frames": self.item_duration(entry),
        }

    def add_generator(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        generator = operation.get("generator")
        if generator not in {"color", "text", "tone", "noise"}:
            raise ToolError("generator must be color, text, tone, or noise.")
        duration = _int(operation.get("duration_frames"), "duration_frames", 1)
        producer_id = self.new_id("producer")
        producer = ET.Element(
            "producer", {"id": producer_id, "in": "0", "out": str(duration - 1)}
        )
        _set_property(producer, "length", duration)
        _set_property(producer, "eof", "pause")
        if generator in {"color", "text"}:
            color = operation.get(
                "color", "#00000000" if generator == "text" else "#000000"
            )
            _set_property(producer, "resource", color)
            _set_property(producer, "mlt_service", "color")
            _set_property(producer, "mlt_image_format", "rgba")
        elif generator == "tone":
            _set_property(producer, "resource", "tone:")
            _set_property(producer, "mlt_service", "tone")
            _set_property(producer, "frequency", operation.get("frequency", 440))
            _set_property(producer, "level", operation.get("level", -12))
        else:
            _set_property(producer, "resource", "noise:")
            _set_property(producer, "mlt_service", "noise")
        if generator == "text":
            text = operation.get("text")
            if not isinstance(text, str) or not text:
                raise ToolError("text must be a non-empty string.")
            filter_element = ET.SubElement(
                producer,
                "filter",
                {"id": self.new_id("filter"), "in": "0", "out": str(duration - 1)},
            )
            defaults = {
                "mlt_service": "dynamictext",
                "shotcut:filter": "dynamicText",
                "argument": text,
                "geometry": "10%/10%:80%x80%",
                "family": "Verdana" if os.name == "nt" else "Sans",
                "size": max(24, int(self.profile().get("height", "1080")) // 18),
                "fgcolour": "#ffffffff",
                "bgcolour": "#00000000",
                "olcolour": "#aa000000",
                "outline": 3,
                "halign": "center",
                "valign": "middle",
            }
            properties = operation.get("properties", {})
            if not isinstance(properties, dict):
                raise ToolError("Text generator properties must be an object.")
            defaults.update(properties)
            for name, value in defaults.items():
                _set_property(filter_element, name, value)
        entry = ET.Element(
            "entry", {"producer": producer_id, "in": "0", "out": str(duration - 1)}
        )
        self.insert_root_before_main(producer)
        position = operation.get("position_frame")
        self.place_item(
            track.playlist,
            entry,
            _int(position, "position_frame", 0) if position is not None else None,
            operation.get("mode", "insert"),
        )
        return {
            "producer_id": producer_id,
            "track_id": track.id,
            "duration_frames": duration,
        }

    def _item(
        self, track: TrackRef, index: Any
    ) -> tuple[list[ET.Element], int, ET.Element]:
        item_index = _int(index, "item_index", 0)
        sequence = self.sequence(track.playlist)
        if item_index >= len(sequence):
            raise ToolError(
                f"item_index {item_index} is out of range for track {track.name}."
            )
        return sequence, item_index, sequence[item_index]

    def remove_item(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        sequence, index, item = self._item(track, operation.get("item_index"))
        if self.is_transition(item):
            raise ToolError("Use remove_transition to remove a transition.")
        duration = self.item_duration(item)
        removed_service_id = item.get("producer", "") if item.tag == "entry" else ""
        ripple = operation.get("ripple", False)
        if not isinstance(ripple, bool):
            raise ToolError("ripple must be a boolean.")
        sequence[index : index + 1] = (
            [] if ripple else [ET.Element("blank", {"length": str(duration)})]
        )
        self.replace_sequence(track.playlist, self.consolidate_blanks(sequence))
        self.remove_unreferenced_services({removed_service_id})
        self.update_main_duration()
        return {"removed": True, "duration_frames": duration, "ripple": ripple}

    def trim_item(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        _, _, item = self._item(track, operation.get("item_index"))
        if item.tag != "entry" or self.is_transition(item):
            raise ToolError("Only regular clips can be trimmed.")
        frame_in = _clock_to_frames(item.get("in"), self.fps) or 0
        frame_out = _clock_to_frames(item.get("out"), self.fps)
        if frame_out is None:
            frame_out = frame_in + self.item_duration(item) - 1
        new_in = _int(operation.get("in_frame", frame_in), "in_frame", 0)
        new_out = _int(operation.get("out_frame", frame_out), "out_frame", 0)
        if new_out < new_in:
            raise ToolError("out_frame must be greater than or equal to in_frame.")
        producer = self.id_map().get(item.get("producer", ""))
        producer_out = (
            _clock_to_frames(producer.get("out"), self.fps)
            if producer is not None
            else None
        )
        if producer_out is not None and new_out > producer_out:
            raise ToolError(f"out_frame exceeds the end of the media ({producer_out}).")
        item.set("in", str(new_in))
        item.set("out", str(new_out))
        self.update_main_duration()
        return {"trimmed": True, "in_frame": new_in, "out_frame": new_out}

    def split_item(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        sequence, index, item = self._item(track, operation.get("item_index"))
        if item.tag != "entry" or self.is_transition(item):
            raise ToolError("Only regular clips can be split.")
        offset = _int(operation.get("offset_frame"), "offset_frame", 1)
        duration = self.item_duration(item)
        if offset >= duration:
            raise ToolError(
                f"offset_frame must be less than the duration ({duration})."
            )
        frame_in = _clock_to_frames(item.get("in"), self.fps) or 0
        left = copy.deepcopy(item)
        right = copy.deepcopy(item)
        left.set("out", str(frame_in + offset - 1))
        right.set("in", str(frame_in + offset))
        original = self.id_map().get(item.get("producer", ""))
        if original is not None and original.tag in {"producer", "chain"}:
            clone = self.clone_service(original)
            right.set("producer", clone.get("id", ""))
            self.insert_root_before_main(clone)
        sequence[index : index + 1] = [left, right]
        self.replace_sequence(track.playlist, sequence)
        return {"split": True, "left_index": index, "right_index": index + 1}

    def move_item(self, operation: dict[str, Any]) -> dict[str, Any]:
        source_track = self.find_track(operation.get("track"))
        target_track = self.find_track(
            operation.get("target_track", operation.get("track"))
        )
        sequence, index, item = self._item(source_track, operation.get("item_index"))
        if item.tag != "entry" or self.is_transition(item):
            raise ToolError("Only regular clips can be moved.")
        if (index > 0 and self.is_transition(sequence[index - 1])) or (
            index + 1 < len(sequence) and self.is_transition(sequence[index + 1])
        ):
            raise ToolError("Remove the adjacent transition before moving this clip.")
        duration = self.item_duration(item)
        ripple_source = _boolean(operation.get("ripple_source", False), "ripple_source")
        sequence[index : index + 1] = (
            [] if ripple_source else [ET.Element("blank", {"length": str(duration)})]
        )
        self.replace_sequence(source_track.playlist, self.consolidate_blanks(sequence))
        position = _int(operation.get("position_frame"), "position_frame", 0)
        self.place_item(
            target_track.playlist, item, position, operation.get("mode", "overwrite")
        )
        return {
            "moved": True,
            "target_track": target_track.id,
            "position_frame": position,
        }

    def insert_gap(self, operation: dict[str, Any]) -> dict[str, Any]:
        duration = _int(operation.get("duration_frames"), "duration_frames", 1)
        position = _int(operation.get("position_frame"), "position_frame", 0)
        selectors = operation.get("tracks")
        tracks = (
            self.tracks()
            if selectors in (None, "all")
            else [self.find_track(item) for item in selectors]
        )
        for track in tracks:
            self.place_item(
                track.playlist,
                ET.Element("blank", {"length": str(duration)}),
                position,
                "insert",
            )
        return {"inserted_gap_frames": duration, "track_count": len(tracks)}

    def remove_range(self, operation: dict[str, Any]) -> dict[str, Any]:
        start = _int(operation.get("position_frame"), "position_frame", 0)
        duration = _int(operation.get("duration_frames"), "duration_frames", 1)
        ripple = operation.get("ripple", True)
        if not isinstance(ripple, bool):
            raise ToolError("ripple must be a boolean.")
        selectors = operation.get("tracks")
        tracks = (
            self.tracks()
            if selectors in (None, "all")
            else [self.find_track(item) for item in selectors]
        )
        for track in tracks:
            sequence = self.sequence(track.playlist)
            left = self.split_sequence_at(sequence, start)
            right = self.split_sequence_at(sequence, start + duration)
            if any(self.is_transition(item) for item in sequence[left:right]):
                raise ToolError("The range contains a transition; remove it first.")
            replacement = (
                [] if ripple else [ET.Element("blank", {"length": str(duration)})]
            )
            sequence[left:right] = replacement
            self.replace_sequence(track.playlist, self.consolidate_blanks(sequence))
        self.update_main_duration()
        return {
            "removed_range_frames": duration,
            "track_count": len(tracks),
            "ripple": ripple,
        }

    def add_transition(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        sequence, left_index, left = self._item(track, operation.get("left_item_index"))
        if left_index + 1 >= len(sequence):
            raise ToolError(
                "There is no clip to the right for creating the transition."
            )
        right = sequence[left_index + 1]
        if (
            left.tag != "entry"
            or right.tag != "entry"
            or self.is_transition(left)
            or self.is_transition(right)
        ):
            raise ToolError("A transition requires two adjacent regular clips.")
        duration = _int(operation.get("duration_frames"), "duration_frames", 1)
        left_duration, right_duration = (
            self.item_duration(left),
            self.item_duration(right),
        )
        if duration >= left_duration or duration >= right_duration:
            raise ToolError("The transition must be shorter than both clips.")
        left_in = _clock_to_frames(left.get("in"), self.fps) or 0
        left_out = (
            _clock_to_frames(left.get("out"), self.fps) or left_in + left_duration - 1
        )
        right_in = _clock_to_frames(right.get("in"), self.fps) or 0
        pre = copy.deepcopy(left)
        pre.set("out", str(left_out - duration))
        post = copy.deepcopy(right)
        post.set("in", str(right_in + duration))
        tractor_id = self.new_id("tractor_transition")
        tractor = ET.Element(
            "tractor", {"id": tractor_id, "in": "0", "out": str(duration - 1)}
        )
        _set_property(tractor, "shotcut:transition", 1)
        _set_property(tractor, "shotcut:caption", operation.get("name", "Transition"))
        ET.SubElement(
            tractor,
            "track",
            {
                "producer": left.get("producer", ""),
                "in": str(left_out - duration + 1),
                "out": str(left_out),
            },
        )
        ET.SubElement(
            tractor,
            "track",
            {
                "producer": right.get("producer", ""),
                "in": str(right_in),
                "out": str(right_in + duration - 1),
            },
        )
        video_service = operation.get("service", "luma")
        if not isinstance(video_service, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:+-]+", video_service
        ):
            raise ToolError("Invalid transition service.")
        video = self._transition(video_service, 0, 1)
        video.set("out", str(duration - 1))
        properties = operation.get("properties", {})
        if not isinstance(properties, dict):
            raise ToolError("Transition properties must be an object.")
        for name, value in properties.items():
            _set_property(video, name, value)
        tractor.append(video)
        audio_crossfade = _boolean(
            operation.get("audio_crossfade", True), "audio_crossfade"
        )
        if audio_crossfade:
            audio = self._transition("mix", 0, 1, start=-2, accepts_blanks=1)
            audio.set("out", str(duration - 1))
            tractor.append(audio)
        self.insert_root_before_main(tractor)
        transition_entry = ET.Element(
            "entry", {"producer": tractor_id, "in": "0", "out": str(duration - 1)}
        )
        sequence[left_index : left_index + 2] = [pre, transition_entry, post]
        self.replace_sequence(track.playlist, sequence)
        self.update_main_duration()
        return {
            "transition_id": tractor_id,
            "item_index": left_index + 1,
            "duration_frames": duration,
        }

    def remove_transition(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        sequence, index, entry = self._item(track, operation.get("item_index"))
        if not self.is_transition(entry) or index == 0 or index + 1 >= len(sequence):
            raise ToolError("item_index does not point to a removable transition.")
        tractor = self.id_map().get(entry.get("producer", ""))
        assert tractor is not None
        nested_tracks = tractor.findall("track")
        nested_transitions = tractor.findall("transition")
        allowed_tags = {"property", "track", "transition"}
        duration = self.item_duration(entry)
        nested_durations = []
        for item in nested_tracks:
            nested_in = _clock_to_frames(item.get("in"), self.fps)
            nested_out = _clock_to_frames(item.get("out"), self.fps)
            nested_durations.append(
                nested_out - (nested_in or 0) + 1 if nested_out is not None else None
            )
        signature_is_known = (
            len(nested_tracks) == 2
            and 1 <= len(nested_transitions) <= 2
            and all(child.tag in allowed_tags for child in tractor)
            and nested_tracks[0].get("producer") == sequence[index - 1].get("producer")
            and nested_tracks[1].get("producer") == sequence[index + 1].get("producer")
            and all(
                _property(item, "a_track") == "0" and _property(item, "b_track") == "1"
                for item in nested_transitions
            )
            and all(item_duration == duration for item_duration in nested_durations)
        )
        if not signature_is_known:
            raise ToolError("The transition structure is not recognized.")
        left, right = (
            copy.deepcopy(sequence[index - 1]),
            copy.deepcopy(sequence[index + 1]),
        )
        if left.tag != "entry" or right.tag != "entry":
            raise ToolError("The transition does not have recognizable adjacent clips.")
        left.set("out", nested_tracks[0].get("out", left.get("out", "0")))
        right.set("in", nested_tracks[1].get("in", right.get("in", "0")))
        sequence[index - 1 : index + 2] = [left, right]
        self.replace_sequence(track.playlist, sequence)
        self.root.remove(tractor)
        self.invalidate()
        self.update_main_duration()
        return {"removed_transition": entry.get("producer")}

    def _filter_host(self, operation: dict[str, Any]) -> ET.Element:
        target = operation.get("target", "project")
        if target == "project":
            return self.main_tractor()
        track = self.find_track(operation.get("track"))
        if target == "track":
            return track.playlist
        if target == "clip":
            _, _, entry = self._item(track, operation.get("item_index"))
            if entry.tag != "entry":
                raise ToolError("The selected item is not a clip.")
            return self.isolate_entry_service(entry)
        raise ToolError("target must be project, track, or clip.")

    def add_filter(self, operation: dict[str, Any]) -> dict[str, Any]:
        host = self._filter_host(operation)
        service = operation.get("service")
        if not isinstance(service, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:+-]+", service
        ):
            raise ToolError("Invalid filter service.")
        filter_id = self.new_id("filter")
        attrs = {"id": filter_id}
        if "in_frame" in operation:
            attrs["in"] = str(_int(operation["in_frame"], "in_frame", 0))
        if "out_frame" in operation:
            attrs["out"] = str(_int(operation["out_frame"], "out_frame", 0))
        element = ET.Element("filter", attrs)
        _set_property(element, "mlt_service", service)
        shotcut_filter = operation.get("shotcut_filter")
        if shotcut_filter:
            _set_property(element, "shotcut:filter", shotcut_filter)
        properties = operation.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 200:
            raise ToolError("properties must be an object with at most 200 properties.")
        for name, value in properties.items():
            if not isinstance(name, str) or not name:
                raise ToolError("Invalid filter property name.")
            if isinstance(value, (dict, list)):
                raise ToolError(
                    f"properties.{name} must be a scalar; animations use MLT strings."
                )
            _set_property(element, name, value)
        host.append(element)
        self.invalidate()
        return {
            "filter_id": filter_id,
            "service": service,
            "target": operation.get("target", "project"),
        }

    def update_filter(self, operation: dict[str, Any]) -> dict[str, Any]:
        filter_id = operation.get("filter_id")
        element = self.id_map().get(filter_id) if isinstance(filter_id, str) else None
        if element is None or element.tag != "filter":
            raise ToolError(f"Filter not found: {filter_id}")
        if "enabled" in operation:
            enabled = operation["enabled"]
            if not isinstance(enabled, bool):
                raise ToolError("enabled must be a boolean.")
            _set_property(element, "disable", 0 if enabled else 1)
        for attr, label in (("in_frame", "in"), ("out_frame", "out")):
            if attr in operation:
                element.set(label, str(_int(operation[attr], attr, 0)))
        properties = operation.get("properties", {})
        if not isinstance(properties, dict):
            raise ToolError("properties must be an object.")
        for name, value in properties.items():
            if value is None:
                _remove_property(element, name)
            elif isinstance(value, (str, int, float, bool)):
                _set_property(
                    element,
                    name,
                    1 if value is True else 0 if value is False else value,
                )
            else:
                raise ToolError(f"properties.{name} must be a scalar or null.")
        return {"filter_id": filter_id, "updated": True}

    def remove_filter(self, operation: dict[str, Any]) -> dict[str, Any]:
        filter_id = operation.get("filter_id")
        element = self.id_map().get(filter_id) if isinstance(filter_id, str) else None
        if element is None or element.tag != "filter":
            raise ToolError(f"Filter not found: {filter_id}")
        parent = next(
            (node for node in self.root.iter() if element in list(node)), None
        )
        if parent is None:
            raise ToolError("The filter's XML parent was not found.")
        parent.remove(element)
        self.invalidate()
        return {"filter_id": filter_id, "removed": True}

    def set_notes(self, operation: dict[str, Any]) -> dict[str, Any]:
        notes = operation.get("notes", "")
        if not isinstance(notes, str):
            raise ToolError("notes must be a string.")
        if notes:
            _set_property(self.main_tractor(), "shotcut:projectNote", notes)
        else:
            _remove_property(self.main_tractor(), "shotcut:projectNote")
        return {"notes_updated": True, "length": len(notes)}

    def markers_container(self, create: bool = False) -> ET.Element | None:
        main = self.main_tractor()
        for child in main.findall("properties"):
            if child.get("name") == "shotcut:markers":
                return child
        if create:
            container = ET.Element("properties", {"name": "shotcut:markers"})
            first_non_property = next(
                (
                    index
                    for index, child in enumerate(main)
                    if child.tag not in {"property", "properties"}
                ),
                len(main),
            )
            main.insert(first_non_property, container)
            return container
        return None

    def add_marker(self, operation: dict[str, Any]) -> dict[str, Any]:
        container = self.markers_container(create=True)
        assert container is not None
        keys = [
            int(item.get("name", "-1"))
            for item in container.findall("properties")
            if item.get("name", "").isdigit()
        ]
        key = max(keys, default=-1) + 1
        marker = ET.SubElement(container, "properties", {"name": str(key)})
        start = _int(operation.get("start_frame"), "start_frame", 0)
        end = _int(operation.get("end_frame", start), "end_frame", start)
        text = operation.get("text", f"Marker {key + 1}")
        color = operation.get("color", "#00A0FF")
        if not isinstance(text, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise ToolError("Invalid marker text or color.")
        _set_property(marker, "text", text)
        _set_property(marker, "start", _frames_to_clock(start, self.fps))
        _set_property(marker, "end", _frames_to_clock(end, self.fps))
        _set_property(marker, "color", color.upper())
        return {"marker_id": str(key), "start_frame": start, "end_frame": end}

    def remove_marker(self, operation: dict[str, Any]) -> dict[str, Any]:
        marker_id = str(operation.get("marker_id", ""))
        container = self.markers_container()
        marker = (
            next(
                (
                    item
                    for item in container.findall("properties")
                    if item.get("name") == marker_id
                ),
                None,
            )
            if container is not None
            else None
        )
        if marker is None:
            raise ToolError(f"Marker not found: {marker_id}")
        assert container is not None
        container.remove(marker)
        return {"marker_id": marker_id, "removed": True}

    def set_subtitle_track(self, operation: dict[str, Any]) -> dict[str, Any]:
        name = operation.get("name")
        lang = operation.get("language", "por")
        items = operation.get("items")
        if not isinstance(name, str) or not name.strip() or not isinstance(lang, str):
            raise ToolError("name and language must be non-empty strings.")
        if not isinstance(items, list):
            raise ToolError("items must be a list.")
        main = self.main_tractor()
        feed = next(
            (
                child
                for child in main.findall("filter")
                if _property(child, "mlt_service") == "subtitle_feed"
                and _property(child, "feed") == name
            ),
            None,
        )
        if feed is None:
            feed = ET.Element("filter", {"id": self.new_id("filter_subtitle")})
            _set_property(feed, "mlt_service", "subtitle_feed")
            _set_property(feed, "shotcut:hidden", 1)
            main.append(feed)
        _set_property(feed, "feed", name)
        _set_property(feed, "lang", lang)
        _set_property(feed, "text", _srt(items))
        burn_in = _boolean(operation.get("burn_in", False), "burn_in")
        if burn_in:
            burn = next(
                (
                    child
                    for child in main.findall("filter")
                    if _property(child, "mlt_service") == "subtitle"
                    and _property(child, "feed") == name
                ),
                None,
            )
            if burn is None:
                burn = ET.Element("filter", {"id": self.new_id("filter_subtitle_burn")})
                main.append(burn)
            defaults = {
                "mlt_service": "subtitle",
                "shotcut:filter": "subtitles",
                "feed": name,
                "family": "Verdana" if os.name == "nt" else "Sans",
                "fgcolour": "#ffffffff",
                "bgcolour": "#00000000",
                "olcolour": "#aa000000",
                "outline": 3,
                "size": max(24, int(self.profile().get("height", "1080")) // 20),
                "geometry": "20%/75%:60%x20%",
                "valign": "bottom",
                "halign": "center",
            }
            style = operation.get("style", {})
            if not isinstance(style, dict):
                raise ToolError("style must be an object.")
            defaults.update(style)
            for key, value in defaults.items():
                _set_property(burn, key, value)
        else:
            for child in list(main.findall("filter")):
                if (
                    _property(child, "mlt_service") == "subtitle"
                    and _property(child, "feed") == name
                ):
                    main.remove(child)
        return {"subtitle_track": name, "language": lang, "item_count": len(items)}

    def remove_subtitle_track(self, operation: dict[str, Any]) -> dict[str, Any]:
        name = operation.get("name")
        if not isinstance(name, str):
            raise ToolError("name must be a string.")
        main = self.main_tractor()
        removed = 0
        for child in list(main.findall("filter")):
            if _property(child, "feed") == name and _property(child, "mlt_service") in {
                "subtitle_feed",
                "subtitle",
            }:
                main.remove(child)
                removed += 1
        if not removed:
            raise ToolError(f"Subtitle track not found: {name}")
        self.invalidate()
        return {"subtitle_track": name, "removed_filters": removed}

    def relink_media(self, operation: dict[str, Any]) -> dict[str, Any]:
        old = operation.get("from")
        new = operation.get("to")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("from and to must be strings.")
        new_path = Path(os.path.expandvars(new)).expanduser().resolve()
        if not new_path.exists():
            raise ToolError(f"New resource not found: {new_path}")
        match_basename = _boolean(
            operation.get("match_basename", False), "match_basename"
        )
        allow_multiple = _boolean(
            operation.get("allow_multiple", False), "allow_multiple"
        )
        matches: list[ET.Element] = []
        for element in [*self.root.findall("producer"), *self.root.findall("chain")]:
            resource = _property(element, "resource")
            if resource == old or (
                resource and Path(resource).name == old and match_basename
            ):
                matches.append(element)
        if not matches:
            raise ToolError(f"No resource matches {old!r}.")
        if match_basename and len(matches) > 1 and not allow_multiple:
            raise ToolError(
                f"The basename {old!r} matches {len(matches)} resources; "
                "use the full path or allow_multiple=true."
            )
        for element in matches:
            _set_property(element, "resource", str(new_path).replace("\\", "/"))
        return {"relinked": len(matches), "to": str(new_path)}

    def set_profile(self, operation: dict[str, Any]) -> dict[str, Any]:
        if not _boolean(
            operation.get("preserve_frame_numbers", False),
            "preserve_frame_numbers",
        ):
            raise ToolError(
                "set_profile requires preserve_frame_numbers=true to confirm the timing change."
            )
        profile = self.profile()
        old_fps = self.fps
        requested_num = operation.get(
            "frame_rate_num", int(profile.get("frame_rate_num", "25"))
        )
        requested_den = operation.get(
            "frame_rate_den", int(profile.get("frame_rate_den", "1"))
        )
        new_fps = _int(requested_num, "frame_rate_num", 1) / _int(
            requested_den, "frame_rate_den", 1
        )
        if not math.isclose(old_fps, new_fps):
            markers = self.markers_container()
            if markers is not None:
                for marker in markers.findall("properties"):
                    for prop in marker.findall("property"):
                        if prop.get("name") in {"start", "end"} and prop.text:
                            frames = _clock_to_frames(prop.text, old_fps)
                            if frames is not None:
                                prop.text = str(frames)
            for element in self.root.iter():
                for attribute in ("in", "out", "length"):
                    value = element.get(attribute)
                    if value is not None and not value.isdigit():
                        frames = _clock_to_frames(value, old_fps)
                        if frames is not None:
                            element.set(attribute, str(frames))
                for prop in element.findall("property"):
                    if prop.get("name") == "length" and prop.text:
                        frames = _clock_to_frames(prop.text, old_fps)
                        if frames is not None:
                            prop.text = str(frames)
        for key in (
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
        ):
            if key in operation:
                minimum = (
                    1
                    if key
                    in {
                        "width",
                        "height",
                        "frame_rate_num",
                        "frame_rate_den",
                        "sample_aspect_den",
                        "display_aspect_den",
                    }
                    else 0
                )
                profile.set(key, str(_int(operation[key], key, minimum)))
        return {"profile_updated": True, "fps": self.fps}

    def apply_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(operation, dict):
            raise ToolError("Each operation must be an object.")
        name = operation.get("op")
        handlers = {
            "add_track": self.add_track,
            "remove_track": self.remove_track,
            "update_track": self.update_track,
            "move_track": self.move_track,
            "add_clip": self.add_clip,
            "add_generator": self.add_generator,
            "remove_item": self.remove_item,
            "trim_item": self.trim_item,
            "split_item": self.split_item,
            "move_item": self.move_item,
            "insert_gap": self.insert_gap,
            "remove_range": self.remove_range,
            "add_transition": self.add_transition,
            "remove_transition": self.remove_transition,
            "add_filter": self.add_filter,
            "update_filter": self.update_filter,
            "remove_filter": self.remove_filter,
            "set_notes": self.set_notes,
            "add_marker": self.add_marker,
            "remove_marker": self.remove_marker,
            "set_subtitle_track": self.set_subtitle_track,
            "remove_subtitle_track": self.remove_subtitle_track,
            "relink_media": self.relink_media,
            "set_profile": self.set_profile,
        }
        if not isinstance(name, str):
            raise ToolError("Every operation requires a textual op field.")
        handler = handlers.get(name)
        if handler is None:
            raise ToolError(
                f"Unknown operation: {name}. Options: {', '.join(handlers)}"
            )
        result = handler(operation)
        return {"op": name, **result}

    def _resource_path(self, resource: str) -> Path | None:
        if not resource or resource.startswith(
            ("color:", "colour:", "noise:", "tone:")
        ):
            return None
        if re.match(
            r"^[A-Za-z][A-Za-z0-9+.-]*://", resource
        ) and not resource.startswith("file://"):
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
        xml_root = self.root.get("root")
        base = Path(xml_root) if xml_root else self.path.parent
        if not base.is_absolute():
            base = self.path.parent / base
        return (base / candidate).resolve()

    def filter_summaries(self, host: ET.Element) -> list[dict[str, Any]]:
        return [
            {
                "filter_id": child.get("id"),
                "service": _property(child, "mlt_service"),
                "shotcut_filter": _property(child, "shotcut:filter"),
                "enabled": _property(child, "disable") != "1",
                "properties": _properties(child),
            }
            for child in host.findall("filter")
        ]

    def snapshot(self) -> dict[str, Any]:
        resources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for element in [*self.root.findall("producer"), *self.root.findall("chain")]:
            resource = _property(element, "resource")
            service = _property(element, "mlt_service")
            if (
                not resource
                or resource in seen
                or service in {"color", "colour", "noise", "tone"}
            ):
                continue
            seen.add(resource)
            path = self._resource_path(resource)
            resources.append(
                {
                    "resource": resource,
                    "resolved_path": str(path) if path else None,
                    "exists": path.exists() if path else None,
                }
            )
        tracks: list[dict[str, Any]] = []
        for track in self.tracks():
            cursor = 0
            items: list[dict[str, Any]] = []
            for index, item in enumerate(self.sequence(track.playlist)):
                duration = self.item_duration(item)
                summary: dict[str, Any] = {
                    "item_index": index,
                    "type": "gap"
                    if item.tag == "blank"
                    else "transition"
                    if self.is_transition(item)
                    else "clip",
                    "start_frame": cursor,
                    "duration_frames": duration,
                    "end_frame": cursor + duration - 1,
                }
                if item.tag == "entry":
                    producer_id = item.get("producer")
                    producer = self.id_map().get(producer_id or "")
                    summary.update(
                        producer_id=producer_id,
                        in_frame=_clock_to_frames(item.get("in"), self.fps) or 0,
                        out_frame=_clock_to_frames(item.get("out"), self.fps),
                        resource=_property(producer, "resource")
                        if producer is not None
                        else None,
                        caption=_property(producer, "shotcut:caption")
                        if producer is not None
                        else None,
                        filters=self.filter_summaries(producer)
                        if producer is not None
                        else [],
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
                    "properties": _properties(track.playlist),
                    "filters": self.filter_summaries(track.playlist),
                    "items": items,
                }
            )
        marker_container = self.markers_container()
        markers = []
        if marker_container is not None:
            for marker in marker_container.findall("properties"):
                props = _properties(marker)
                markers.append(
                    {
                        "marker_id": marker.get("name"),
                        "text": props.get("text"),
                        "start_frame": _clock_to_frames(props.get("start"), self.fps),
                        "end_frame": _clock_to_frames(props.get("end"), self.fps),
                        "color": props.get("color"),
                    }
                )
        main = self.main_tractor()
        subtitles = [
            {
                "name": _property(child, "feed"),
                "language": _property(child, "lang"),
                "srt": _property(child, "text"),
            }
            for child in main.findall("filter")
            if _property(child, "mlt_service") == "subtitle_feed"
        ]
        profile: dict[str, Any] = dict(self.profile().attrib)
        profile["fps"] = self.fps
        return {
            "path": str(self.path),
            "revision": self.revision,
            "shotcut_editable": _property(main, "shotcut") == "1",
            "profile": profile,
            "notes": _property(main, "shotcut:projectNote"),
            "duration_frames": max(
                (track["duration_frames"] for track in tracks), default=0
            ),
            "tracks": tracks,
            "filters": self.filter_summaries(main),
            "links": [
                {
                    "link_id": link.get("id"),
                    "service": _property(link, "mlt_service"),
                    "properties": _properties(link),
                }
                for link in self.root.findall(".//link")
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
                tag: len(self.root.findall(f".//{tag}"))
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

    def to_bytes(self) -> bytes:
        ET.indent(self.tree, space="  ")
        return ET.tostring(self.root, encoding="utf-8", xml_declaration=True)


def _write_validated(
    document: ProjectDocument,
    *,
    expected_revision: str | None,
    force: bool,
    validate: bool,
    timeout: int,
    create_backup: bool,
) -> dict[str, Any]:
    path = document.path
    data = document.to_bytes()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp{path.suffix}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with project_lock(path):
        current = path.read_bytes() if path.is_file() else None
        current_mode = path.stat().st_mode if current is not None else None
        current_revision = _sha256(current) if current is not None else None
        if current is not None and not force:
            if not expected_revision:
                raise ConflictError(
                    "expected_revision is required to edit an existing project."
                )
            if expected_revision != current_revision:
                raise ConflictError(
                    f"The project changed. Expected {expected_revision}, current {current_revision}."
                )
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if current_mode is not None:
            os.chmod(temporary, current_mode)
        try:
            validation = (
                validate_project_file(temporary, timeout=timeout)
                if validate
                else {"valid": True}
            )
            if not validation.get("valid"):
                raise ToolError(
                    "MLT rejected the edit before the project was replaced: "
                    + str(validation.get("diagnostic") or validation.get("return_code"))
                )
            latest = path.read_bytes() if path.is_file() else None
            latest_revision = _sha256(latest) if latest is not None else None
            if latest_revision != current_revision:
                raise ConflictError(
                    "The project changed while the candidate edit was being validated. "
                    f"Expected {current_revision}, current {latest_revision}."
                )
            backup_path = (
                write_project_backup(path, current)
                if current is not None and create_backup
                else None
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    revision = _sha256(data)
    return {
        "path": str(path),
        "revision": revision,
        "previous_revision": current_revision,
        "backup_path": str(backup_path) if backup_path else None,
        "validation": validation,
    }


def create_project(arguments: dict[str, Any]) -> dict[str, Any]:
    from .platform import expand_path

    path = expand_path(arguments.get("project_path", ""))
    if path.suffix.lower() not in {".mlt", ".xml"}:
        raise ToolError("The project must use the .mlt or .xml extension.")
    overwrite = _boolean(arguments.get("overwrite", False), "overwrite")
    if path.exists() and not overwrite:
        raise ToolError(f"The project already exists: {path}")
    width = _int(arguments.get("width", 1920), "width", 16)
    height = _int(arguments.get("height", 1080), "height", 16)
    fps_num = _int(arguments.get("fps_num", 30), "fps_num", 1)
    fps_den = _int(arguments.get("fps_den", 1), "fps_den", 1)
    document = ProjectDocument.new(
        path,
        width=width,
        height=height,
        fps_num=fps_num,
        fps_den=fps_den,
        title=arguments.get("notes", "")
        if isinstance(arguments.get("notes", ""), str)
        else "",
    )
    results: list[dict[str, Any]] = []
    tracks = arguments.get("tracks", [])
    if not isinstance(tracks, list):
        raise ToolError("tracks must be a list.")
    for track in tracks:
        results.append(document.add_track({"op": "add_track", **track}))
    clips = arguments.get("clips", [])
    if not isinstance(clips, list):
        raise ToolError("clips must be a list.")
    for clip in clips:
        operation = {"op": "add_clip", "track": "V1", **clip}
        results.append(document.add_clip(operation))
    document.update_main_duration()
    saved = _write_validated(
        document,
        expected_revision=_sha256(path.read_bytes()) if path.exists() else None,
        force=overwrite,
        validate=True,
        timeout=_int(arguments.get("timeout_seconds", 60), "timeout_seconds", 1),
        create_backup=path.exists(),
    )
    loaded = ProjectDocument.load(path)
    return {
        "created": True,
        **saved,
        "operation_results": results,
        "project": loaded.snapshot(),
    }


def _build_edit_candidate(arguments: dict[str, Any]) -> EditCandidate:
    from .platform import expand_path

    path = expand_path(arguments.get("project_path", ""))
    operations = arguments.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ToolError("operations must be a non-empty list.")
    if len(operations) > MAX_OPERATIONS:
        raise ToolError(f"A transaction accepts at most {MAX_OPERATIONS} operations.")
    force = _boolean(arguments.get("force", False), "force")
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and not isinstance(expected_revision, str):
        raise ToolError("expected_revision must be a SHA-256 string.")
    document = ProjectDocument.load(path)
    original = document.source
    original_revision = document.revision
    if not force:
        if not expected_revision:
            raise ConflictError(
                "expected_revision is required to edit an existing project."
            )
        if expected_revision != original_revision:
            raise ConflictError(
                f"The project changed. Expected {expected_revision}, current "
                f"{original_revision}."
            )
    document.ensure_shotcut_structure()
    results: list[dict[str, Any]] = []
    for index, operation in enumerate(operations):
        if cancellation_requested():
            raise RequestCancelled("Project edit cancelled by the MCP client.")
        try:
            results.append(document.apply_operation(operation))
        except ToolError as exc:
            raise ToolError(f"Operation {index} failed: {exc}") from exc
    document.update_main_duration()
    return EditCandidate(
        path=path,
        document=document,
        original=original,
        original_revision=original_revision,
        expected_revision=expected_revision,
        force=force,
        timeout=_int(arguments.get("timeout_seconds", 60), "timeout_seconds", 1),
        operation_results=results,
    )


def plan_project_edit(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("force") not in (None, False):
        raise ToolError("force is not supported by plan_project_edit.")
    candidate = _build_edit_candidate(arguments)
    data = candidate.document.to_bytes()
    prospective_revision = _sha256(data)
    temporary = candidate.path.with_name(
        f".{candidate.path.name}.{uuid.uuid4().hex}.plan{candidate.path.suffix}"
    )
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        validation = validate_project_file(temporary, timeout=candidate.timeout)
    finally:
        temporary.unlink(missing_ok=True)
    latest = candidate.path.read_bytes() if candidate.path.is_file() else None
    latest_revision = _sha256(latest) if latest is not None else None
    if latest_revision != candidate.original_revision:
        raise ConflictError(
            "The project changed while the planned edit was being validated."
        )

    maximum_lines = _int(arguments.get("max_diff_lines", 2000), "max_diff_lines", 0)
    maximum_lines = min(maximum_lines, 5000)
    diff_lines = list(
        difflib.unified_diff(
            candidate.original.decode("utf-8", errors="replace").splitlines(),
            data.decode("utf-8", errors="replace").splitlines(),
            fromfile=str(candidate.path),
            tofile=f"{candidate.path} (planned)",
            lineterm="",
        )
    )
    diff_truncated = len(diff_lines) > maximum_lines
    shown_lines = diff_lines[:maximum_lines]
    candidate.document.source = data
    candidate.document.revision = prospective_revision
    return {
        "planned": True,
        "changed": data != candidate.original,
        "project_path": str(candidate.path),
        "base_revision": candidate.original_revision,
        "prospective_revision": prospective_revision,
        "operation_results": candidate.operation_results,
        "validation": validation,
        "project": candidate.document.snapshot(),
        "unified_diff": "\n".join(shown_lines),
        "diff_lines": len(diff_lines),
        "diff_truncated": diff_truncated,
    }


def edit_project(arguments: dict[str, Any]) -> dict[str, Any]:
    candidate = _build_edit_candidate(arguments)
    saved = _write_validated(
        candidate.document,
        expected_revision=candidate.expected_revision,
        force=candidate.force,
        validate=True,
        timeout=candidate.timeout,
        create_backup=True,
    )
    updated = ProjectDocument.load(candidate.path)
    return {
        "edited": True,
        **saved,
        "operation_results": candidate.operation_results,
        "project": updated.snapshot(),
    }


def list_backups(project_path: Path) -> dict[str, Any]:
    backups = []
    for path in list_project_backups(project_path):
        backups.append(
            {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "modified_at": path.stat().st_mtime,
                "revision": _sha256(path.read_bytes()),
            }
        )
    return {
        "project_path": str(project_path),
        "backup_count": len(backups),
        "backups": backups,
    }


def restore_backup(arguments: dict[str, Any]) -> dict[str, Any]:
    from .platform import expand_path

    project_path = expand_path(arguments.get("project_path", ""))
    backup_path = expand_path(arguments.get("backup_path", ""))
    force = _boolean(arguments.get("force", False), "force")
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and not isinstance(expected_revision, str):
        raise ToolError("expected_revision must be a SHA-256 string.")
    if not is_project_backup(project_path, backup_path):
        raise ToolError("backup_path is not one of this project's backups.")
    document = ProjectDocument.load(backup_path)
    document.path = project_path
    return {
        "restored": True,
        **_write_validated(
            document,
            expected_revision=expected_revision,
            force=force,
            validate=True,
            timeout=60,
            create_backup=True,
        ),
    }
