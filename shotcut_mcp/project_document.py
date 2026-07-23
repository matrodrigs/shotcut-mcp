"""Structure-preserving Shotcut MLT document model and edit operations."""

from __future__ import annotations

import copy
import hashlib
import itertools
import math
import os
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from .errors import ToolError
from .media import media_duration, probe_media_raw, shotcut_file_hash
from .mlt_xml import (
    clock_to_frames as _clock_to_frames,
)
from .mlt_xml import (
    properties as _properties,
)
from .mlt_xml import (
    property_value as _property,
)
from .mlt_xml import resource_references

SEQUENCE_TAGS = {"entry", "blank"}
BACKGROUND_ID = "background"
MAIN_BIN_IDS = {"main_bin", "main bin"}
DocumentT = TypeVar("DocumentT", bound="ProjectDocument")
MAX_PROJECT_BYTES = 64 * 1024 * 1024


def project_revision(data: bytes) -> str:
    """Return the canonical content revision used for optimistic concurrency."""

    return hashlib.sha256(data).hexdigest()


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
        self.revision = project_revision(source)
        self._id_cache: dict[str, ET.Element] | None = None

    @classmethod
    def load(cls: type[DocumentT], path: Path) -> DocumentT:
        if not path.is_file():
            raise ToolError(f"Project not found: {path}")
        if path.stat().st_size > MAX_PROJECT_BYTES:
            raise ToolError(
                f"Project exceeds the {MAX_PROJECT_BYTES // (1024 * 1024)} MiB limit."
            )
        source = path.read_bytes()
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        try:
            root = ET.fromstring(source, parser=parser)
        except ET.ParseError as exc:
            raise ToolError(f"Invalid MLT XML: {exc}") from exc
        return cls(path, ET.ElementTree(root), source)

    @classmethod
    def new(
        cls: type[DocumentT],
        path: Path,
        *,
        width: int,
        height: int,
        fps_num: int,
        fps_den: int,
        title: str,
    ) -> DocumentT:
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
        _set_property(main, "shotcut:processingMode", "Native8Cpu")
        _set_property(main, "shotcut:projectNote", title)
        ET.SubElement(main, "track", {"producer": BACKGROUND_ID})
        ET.SubElement(main, "track", {"producer": "playlist_v1"})
        document = cls(path, ET.ElementTree(root), b"")
        document.ensure_default_track_transitions()
        document.source = document.to_bytes()
        document.revision = project_revision(document.source)
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
                    with suppress(ValueError):
                        index = min(index, children.index(playlist))
        self.root.insert(index, element)
        self.invalidate()

    def normalize_root_service_order(self) -> None:
        """Place existing timeline services before editable track playlists."""
        main = self.main_tractor()
        editable_playlists = [track.playlist for track in self.tracks()]
        children = list(self.root)
        anchors = [playlist for playlist in editable_playlists if playlist in children]
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
            ("shotcut:hash", shotcut_file_hash(media_path)),
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

    def duplicate_item(self, operation: dict[str, Any]) -> dict[str, Any]:
        source_track = self.find_track(operation.get("track"))
        target_track = self.find_track(
            operation.get("target_track", operation.get("track"))
        )
        sequence, index, item = self._item(source_track, operation.get("item_index"))
        if item.tag != "entry" or self.is_transition(item):
            raise ToolError("Only regular clips can be duplicated.")
        if (index > 0 and self.is_transition(sequence[index - 1])) or (
            index + 1 < len(sequence) and self.is_transition(sequence[index + 1])
        ):
            raise ToolError(
                "Remove the adjacent transition before duplicating this clip."
            )
        original = self.id_map().get(item.get("producer", ""))
        if original is None or original.tag not in {"producer", "chain"}:
            raise ToolError("The selected clip service cannot be duplicated safely.")
        clone = self.clone_service(original)
        self.insert_root_before_main(clone)
        duplicate = copy.deepcopy(item)
        duplicate.set("producer", clone.get("id", ""))
        raw_position = operation.get("position_frame")
        position = (
            _int(raw_position, "position_frame", 0)
            if raw_position is not None
            else sum(self.item_duration(node) for node in sequence[: index + 1])
            if source_track.id == target_track.id
            else None
        )
        mode = operation.get("mode", "insert")
        self.place_item(target_track.playlist, duplicate, position, mode)
        return {
            "duplicated": True,
            "producer_id": clone.get("id"),
            "source_track": source_track.id,
            "target_track": target_track.id,
            "position_frame": position,
            "duration_frames": self.item_duration(duplicate),
            "mode": mode,
        }

    def replace_item_media(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        sequence, index, item = self._item(track, operation.get("item_index"))
        if item.tag != "entry" or self.is_transition(item):
            raise ToolError("Only regular clips can have their media replaced.")
        original_id = item.get("producer", "")
        original = self.id_map().get(original_id)
        if original is None or original.tag != "producer":
            raise ToolError(
                "replace_item_media currently supports regular producer-backed clips only."
            )
        service = _property(original, "mlt_service")
        if service not in {"avformat", "avformat-novalidate"}:
            raise ToolError(
                "replace_item_media supports avformat-backed media clips only."
            )
        if _property(original, "shotcut:proxy") or _property(
            original, "shotcut:proxyResource"
        ):
            raise ToolError(
                "Proxy-backed clips require a dedicated proxy-aware replacement."
            )
        if (index > 0 and self.is_transition(sequence[index - 1])) or (
            index + 1 < len(sequence) and self.is_transition(sequence[index + 1])
        ):
            raise ToolError(
                "Remove the adjacent transition before replacing this clip's media."
            )
        frame_in = _clock_to_frames(item.get("in"), self.fps) or 0
        frame_out = _clock_to_frames(item.get("out"), self.fps)
        if frame_out is None:
            frame_out = frame_in + self.item_duration(item) - 1
        raw_path = operation.get("path")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string.")
        media_path = Path(os.path.expandvars(raw_path)).expanduser().resolve()
        if not media_path.is_file():
            raise ToolError(f"Media not found: {media_path}")
        payload = probe_media_raw(media_path)
        duration_seconds = media_duration(payload)
        full_frames = (
            max(1, math.ceil(duration_seconds * self.fps))
            if duration_seconds is not None
            else frame_out + 1
        )
        if frame_out >= full_frames:
            raise ToolError(
                f"Replacement media has {full_frames} frames; source frame {frame_out} is required."
            )
        producer = self.isolate_entry_service(item)
        old_path = _property(producer, "resource")
        producer.set("in", "0")
        producer.set("out", str(full_frames - 1))
        _set_property(producer, "length", full_frames)
        _set_property(producer, "resource", str(media_path).replace("\\", "/"))
        _set_property(producer, "mlt_service", "avformat-novalidate")
        _set_property(producer, "seekable", 1)
        _set_property(producer, "shotcut:skipConvert", 1)
        _set_property(producer, "shotcut:hash", shotcut_file_hash(media_path))
        caption = operation.get("caption", media_path.name)
        if not isinstance(caption, str):
            raise ToolError("caption must be a string.")
        _set_property(producer, "shotcut:caption", caption)
        for prop in list(producer.findall("property")):
            name = prop.get("name", "")
            if name in {
                "audio_index",
                "video_index",
                "astream",
                "vstream",
            } or name.startswith("meta."):
                producer.remove(prop)
        return {
            "replaced": True,
            "producer_id": producer.get("id"),
            "track_id": track.id,
            "old_path": old_path,
            "path": str(media_path),
            "in_frame": frame_in,
            "out_frame": frame_out,
            "duration_frames": frame_out - frame_in + 1,
            "shotcut_hash": _property(producer, "shotcut:hash"),
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
        sequence, index, item = self._item(track, operation.get("item_index"))
        if item.tag != "entry" or self.is_transition(item):
            raise ToolError("Only regular clips can be trimmed.")
        frame_in = _clock_to_frames(item.get("in"), self.fps) or 0
        frame_out = _clock_to_frames(item.get("out"), self.fps)
        if frame_out is None:
            frame_out = frame_in + self.item_duration(item) - 1
        edge = operation.get("edge")
        if edge is not None:
            if edge not in {"start", "end"}:
                raise ToolError("edge must be start or end.")
            delta = _int(operation.get("delta"), "delta")
            if delta == 0:
                raise ToolError("delta must not be zero.")
            new_in = frame_in + delta if edge == "start" else frame_in
            new_out = frame_out + delta if edge == "end" else frame_out
        else:
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
        if new_in < 0:
            raise ToolError("in_frame cannot be negative.")
        old_duration = frame_out - frame_in + 1
        new_duration = new_out - new_in + 1
        duration_change = new_duration - old_duration
        ripple = True
        if edge is not None:
            ripple = _boolean(operation.get("ripple", True), "ripple")
            if not ripple:
                side = "before" if edge == "start" else "after"
                self._compensate_trim_blank(sequence, index, side, -duration_change)
        item.set("in", str(new_in))
        item.set("out", str(new_out))
        self.replace_sequence(track.playlist, self.consolidate_blanks(sequence))
        if edge is not None and ripple and operation.get("ripple_markers", False):
            if not isinstance(operation.get("ripple_markers"), bool):
                raise ToolError("ripple_markers must be a boolean.")
            boundary = sum(self.item_duration(node) for node in sequence[:index])
            if edge == "end":
                boundary += old_duration
            self._shift_markers(boundary, duration_change)
        ripple_tracks = operation.get("ripple_tracks")
        if ripple_tracks not in (None, [], False):
            raise ToolError(
                "ripple_tracks is not enabled until locked-track Shotcut fixtures pass."
            )
        self.update_main_duration()
        return {
            "trimmed": True,
            "in_frame": new_in,
            "out_frame": new_out,
            "duration_change_frames": duration_change,
            "ripple": ripple,
        }

    def _compensate_trim_blank(
        self,
        sequence: list[ET.Element],
        item_index: int,
        side: str,
        blank_change: int,
    ) -> None:
        adjacent_index = item_index - 1 if side == "before" else item_index + 1
        if adjacent_index >= 0 and adjacent_index < len(sequence):
            adjacent = sequence[adjacent_index]
            if self.is_transition(adjacent):
                raise ToolError("Non-ripple trim cannot touch a transition.")
        else:
            adjacent = None
        if blank_change > 0:
            if adjacent is not None and adjacent.tag == "blank":
                adjacent.set("length", str(self.item_duration(adjacent) + blank_change))
            else:
                blank = ET.Element("blank", {"length": str(blank_change)})
                sequence.insert(
                    item_index if side == "before" else item_index + 1, blank
                )
        elif blank_change < 0:
            needed = -blank_change
            if adjacent is None or adjacent.tag != "blank":
                raise ToolError(
                    "Non-ripple extension requires an adjacent gap with enough frames."
                )
            available = self.item_duration(adjacent)
            if available < needed:
                raise ToolError(
                    f"Adjacent gap has {available} frames; {needed} are required."
                )
            if available == needed:
                sequence.remove(adjacent)
            else:
                adjacent.set("length", str(available - needed))

    def _shift_markers(self, boundary: int, delta: int) -> None:
        if delta == 0:
            return
        container = self.markers_container()
        if container is None:
            return
        for marker in container.findall("properties"):
            for prop in marker.findall("property"):
                if prop.get("name") not in {"start", "end"} or not prop.text:
                    continue
                frame = _clock_to_frames(prop.text, self.fps)
                if frame is not None and frame >= boundary:
                    prop.text = str(max(0, frame + delta))

    def _regular_clip(
        self, track: TrackRef, item_index: Any
    ) -> tuple[list[ET.Element], int, ET.Element, int, int]:
        sequence, index, item = self._item(track, item_index)
        if item.tag != "entry" or self.is_transition(item):
            raise ToolError("The operation requires a regular clip.")
        frame_in = _clock_to_frames(item.get("in"), self.fps) or 0
        frame_out = _clock_to_frames(item.get("out"), self.fps)
        if frame_out is None:
            frame_out = frame_in + self.item_duration(item) - 1
        return sequence, index, item, frame_in, frame_out

    def _check_source_range(
        self, item: ET.Element, frame_in: int, frame_out: int
    ) -> None:
        if frame_in < 0 or frame_out < frame_in:
            raise ToolError("The requested edit exceeds the source handles.")
        producer = self.id_map().get(item.get("producer", ""))
        producer_out = (
            _clock_to_frames(producer.get("out"), self.fps)
            if producer is not None
            else None
        )
        if producer_out is not None and frame_out > producer_out:
            raise ToolError(
                f"The requested edit exceeds the source end ({producer_out})."
            )

    def slip_item(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        _, _, item, frame_in, frame_out = self._regular_clip(
            track, operation.get("item_index")
        )
        delta = _int(operation.get("delta"), "delta")
        if delta == 0:
            raise ToolError("delta must not be zero.")
        new_in, new_out = frame_in + delta, frame_out + delta
        self._check_source_range(item, new_in, new_out)
        item.set("in", str(new_in))
        item.set("out", str(new_out))
        return {
            "slipped": True,
            "delta_frames": delta,
            "before": {"in_frame": frame_in, "out_frame": frame_out},
            "after": {"in_frame": new_in, "out_frame": new_out},
        }

    def roll_edit(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        sequence, left_index, left, left_in, left_out = self._regular_clip(
            track, operation.get("left_item_index")
        )
        right_index = left_index + 1
        if right_index >= len(sequence):
            raise ToolError("roll_edit requires a contiguous right-hand clip.")
        _, _, right, right_in, right_out = self._regular_clip(track, right_index)
        delta = _int(operation.get("delta"), "delta")
        if delta == 0:
            raise ToolError("delta must not be zero.")
        new_left_out = left_out + delta
        new_right_in = right_in + delta
        self._check_source_range(left, left_in, new_left_out)
        self._check_source_range(right, new_right_in, right_out)
        left.set("out", str(new_left_out))
        right.set("in", str(new_right_in))
        self.update_main_duration()
        return {
            "rolled": True,
            "delta_frames": delta,
            "left": {"in_frame": left_in, "out_frame": new_left_out},
            "right": {"in_frame": new_right_in, "out_frame": right_out},
            "duration_change_frames": 0,
        }

    def slide_item(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        sequence, index, _selected, selected_in, selected_out = self._regular_clip(
            track, operation.get("item_index")
        )
        if index == 0 or index + 1 >= len(sequence):
            raise ToolError("slide_item requires contiguous clips on both sides.")
        _, _, left, left_in, left_out = self._regular_clip(track, index - 1)
        _, _, right, right_in, right_out = self._regular_clip(track, index + 1)
        delta = _int(operation.get("delta"), "delta")
        if delta == 0:
            raise ToolError("delta must not be zero.")
        new_left_out = left_out + delta
        new_right_in = right_in + delta
        self._check_source_range(left, left_in, new_left_out)
        self._check_source_range(right, new_right_in, right_out)
        left.set("out", str(new_left_out))
        right.set("in", str(new_right_in))
        self.update_main_duration()
        return {
            "slid": True,
            "delta_frames": delta,
            "selected": {"in_frame": selected_in, "out_frame": selected_out},
            "left": {"in_frame": left_in, "out_frame": new_left_out},
            "right": {"in_frame": new_right_in, "out_frame": right_out},
            "duration_change_frames": 0,
        }

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

    def move_filter(self, operation: dict[str, Any]) -> dict[str, Any]:
        filter_id = operation.get("filter_id")
        element = self.id_map().get(filter_id) if isinstance(filter_id, str) else None
        if element is None or element.tag != "filter":
            raise ToolError(f"Filter not found: {filter_id}")
        parent = next(
            (node for node in self.root.iter() if element in list(node)), None
        )
        if parent is None or parent.tag not in {
            "producer",
            "chain",
            "playlist",
            "tractor",
        }:
            raise ToolError("The filter is not attached to a supported filter host.")
        filters = parent.findall("filter")
        old_position = filters.index(element)
        before_filter_id = operation.get("before_filter_id")
        if before_filter_id is not None and not isinstance(before_filter_id, str):
            raise ToolError("before_filter_id must be a string.")
        if before_filter_id == filter_id:
            return {
                "filter_id": filter_id,
                "moved": False,
                "old_position": old_position,
                "position": old_position,
            }
        before = (
            self.id_map().get(before_filter_id)
            if isinstance(before_filter_id, str)
            else None
        )
        if before_filter_id is not None and (before is None or before.tag != "filter"):
            raise ToolError(f"Filter not found: {before_filter_id}")
        if before is not None and before not in filters:
            raise ToolError("Both filters must belong to the same host.")
        parent.remove(element)
        remaining = parent.findall("filter")
        if before is not None:
            insertion_index = list(parent).index(before)
        elif remaining:
            insertion_index = list(parent).index(remaining[-1]) + 1
        else:
            insertion_index = next(
                (
                    index
                    for index, child in enumerate(parent)
                    if child.tag not in {"property", "properties"}
                ),
                len(parent),
            )
        parent.insert(insertion_index, element)
        position = parent.findall("filter").index(element)
        return {
            "filter_id": filter_id,
            "moved": True,
            "old_position": old_position,
            "position": position,
            "host_id": parent.get("id"),
        }

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

    def _marker(self, marker_id: str) -> tuple[ET.Element, ET.Element]:
        container = self.markers_container()
        matches = (
            [
                item
                for item in container.findall("properties")
                if item.get("name") == marker_id
            ]
            if container is not None
            else []
        )
        if len(matches) != 1 or container is None:
            raise ToolError(f"Marker not found uniquely: {marker_id}")
        return container, matches[0]

    def update_marker(self, operation: dict[str, Any]) -> dict[str, Any]:
        marker_id = str(operation.get("marker_id", ""))
        mutable = {"start_frame", "end_frame", "text", "color"}
        if not any(name in operation for name in mutable):
            raise ToolError("update_marker requires at least one field to change.")
        _, marker = self._marker(marker_id)
        current_start = _clock_to_frames(_property(marker, "start"), self.fps) or 0
        current_end = _clock_to_frames(_property(marker, "end"), self.fps)
        if current_end is None:
            current_end = current_start
        start = _int(operation.get("start_frame", current_start), "start_frame", 0)
        end_value = operation.get("end_frame", current_end)
        if (
            current_start == current_end
            and "start_frame" in operation
            and "end_frame" not in operation
        ):
            end_value = start
        end = _int(end_value, "end_frame", 0)
        if end < start:
            raise ToolError("end_frame must be greater than or equal to start_frame.")
        before = {
            "text": _property(marker, "text"),
            "start_frame": current_start,
            "end_frame": current_end,
            "color": _property(marker, "color"),
        }
        if "text" in operation:
            text = operation["text"]
            if not isinstance(text, str):
                raise ToolError("text must be a string.")
            _set_property(marker, "text", text)
        if "color" in operation:
            color = operation["color"]
            if not isinstance(color, str) or not re.fullmatch(
                r"#[0-9A-Fa-f]{6}", color
            ):
                raise ToolError("color must use #RRGGBB.")
            _set_property(marker, "color", color.upper())
        _set_property(marker, "start", _frames_to_clock(start, self.fps))
        _set_property(marker, "end", _frames_to_clock(end, self.fps))
        return {
            "marker_id": marker_id,
            "updated": True,
            "before": before,
            "after": {
                "text": _property(marker, "text"),
                "start_frame": start,
                "end_frame": end,
                "color": _property(marker, "color"),
            },
        }

    def remove_marker(self, operation: dict[str, Any]) -> dict[str, Any]:
        marker_id = str(operation.get("marker_id", ""))
        container, marker = self._marker(marker_id)
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

    def set_color_workflow(self, operation: dict[str, Any]) -> dict[str, Any]:
        workflow = operation.get("workflow")
        if workflow not in {"sdr", "hlg", "pq"}:
            raise ToolError("workflow must be sdr, hlg, or pq.")
        allowed_modes = {
            "sdr": {"Native8Cpu", "Native10Cpu", "Linear10Cpu", "Linear10GpuCpu"},
            "hlg": {"Native10Cpu", "Linear10Cpu", "Linear10GpuCpu"},
            "pq": {"Native10Cpu", "Linear10Cpu", "Linear10GpuCpu"},
        }
        default_mode = "Native8Cpu" if workflow == "sdr" else "Native10Cpu"
        processing_mode = operation.get("processing_mode", default_mode)
        if processing_mode not in allowed_modes[workflow]:
            options = ", ".join(sorted(allowed_modes[workflow]))
            raise ToolError(
                f"processing_mode {processing_mode!r} is incompatible with {workflow}; "
                f"options: {options}."
            )
        main = self.main_tractor()
        profile = self.profile()
        _set_property(main, "shotcut:processingMode", processing_mode)
        if workflow == "sdr":
            _remove_property(main, "shotcut:colorTransfer")
            colorspace = operation.get("colorspace", 709)
            if colorspace not in {601, 709}:
                raise ToolError("SDR colorspace must be 601 or 709.")
        else:
            transfer = "arib-std-b67" if workflow == "hlg" else "smpte2084"
            _set_property(main, "shotcut:colorTransfer", transfer)
            colorspace = 2020
        profile.set("colorspace", str(colorspace))
        return {
            "color_workflow_updated": True,
            "workflow": workflow,
            "processing_mode": processing_mode,
            "color_transfer": _property(main, "shotcut:colorTransfer"),
            "colorspace": str(colorspace),
        }

    def set_clip_speed(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        _, _, entry, frame_in, frame_out = self._regular_clip(
            track, operation.get("item_index")
        )
        speed = _number(operation.get("speed"), "speed")
        if speed == 0 or not 0.05 <= abs(speed) <= 100:
            raise ToolError("speed must be between -100 and 100 and not cross zero.")
        pitch = _boolean(
            operation.get("pitch_compensation", True), "pitch_compensation"
        )
        service = self.isolate_entry_service(entry)
        if service.tag not in {"producer", "chain"}:
            raise ToolError("Constant speed requires a producer or chain clip.")
        if service.findall("link"):
            raise ToolError("Constant speed cannot replace an existing chain link.")
        current_service = _property(service, "mlt_service")
        if current_service not in {"avformat", "avformat-novalidate", "timewarp"}:
            raise ToolError(
                "Constant speed is supported only for ordinary media clips."
            )
        original_resource = _property(service, "shotcut:mcpOriginalResource")
        if not original_resource:
            original_resource = _property(service, "warp_resource")
        if not original_resource:
            resource = _property(service, "resource") or ""
            match = re.match(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+):(.+)$", resource)
            original_resource = match.group(1) if match else resource
        if not original_resource:
            raise ToolError("The clip does not expose a timewarp-compatible resource.")
        original_in = int(_property(service, "shotcut:mcpOriginalIn") or frame_in)
        original_out = int(_property(service, "shotcut:mcpOriginalOut") or frame_out)
        source_out = _clock_to_frames(service.get("out"), self.fps)
        original_length = int(
            _property(service, "shotcut:mcpOriginalLength")
            or ((source_out + 1) if source_out is not None else original_out + 1)
        )
        if current_service != "timewarp":
            _set_property(
                service,
                "shotcut:mcpOriginalService",
                current_service or "avformat-novalidate",
            )
        _set_property(service, "shotcut:mcpOriginalResource", original_resource)
        _set_property(service, "shotcut:mcpOriginalIn", original_in)
        _set_property(service, "shotcut:mcpOriginalOut", original_out)
        _set_property(service, "shotcut:mcpOriginalLength", original_length)
        speed_text = f"{speed:g}"
        _set_property(service, "resource", f"{speed_text}:{original_resource}")
        _set_property(service, "warp_resource", original_resource)
        _set_property(service, "warp_speed", speed_text)
        _set_property(service, "warp_pitch", 1 if pitch else 0)
        _set_property(service, "mlt_service", "timewarp")
        warped_length = max(1, math.ceil(original_length / abs(speed)))
        warped_in = max(0, math.floor(original_in / abs(speed)))
        duration = max(1, math.ceil((original_out - original_in + 1) / abs(speed)))
        warped_out = warped_in + duration - 1
        service.set("in", "0")
        service.set("out", str(warped_length - 1))
        _set_property(service, "length", warped_length)
        entry.set("in", str(warped_in))
        entry.set("out", str(warped_out))
        self.update_main_duration()
        return {
            "speed_updated": True,
            "speed": speed,
            "pitch_compensation": pitch,
            "duration_frames": duration,
            "in_frame": warped_in,
            "out_frame": warped_out,
        }

    @staticmethod
    def _speed_map_duration(
        source_frames: int, keyframes: list[tuple[int, float]]
    ) -> int:
        remaining = float(source_frames)
        for (start, speed), (end, next_speed) in itertools.pairwise(keyframes):
            span = end - start
            slope = (next_speed - speed) / span
            area = span * (speed + next_speed) / 2
            if remaining > area:
                remaining -= area
                continue
            if abs(slope) < 1e-12:
                partial = remaining / speed
            else:
                discriminant = speed * speed + 2 * slope * remaining
                partial = (-speed + math.sqrt(max(0.0, discriminant))) / slope
            return max(1, math.ceil(start + partial))
        start, speed = keyframes[-1]
        return max(1, math.ceil(start + remaining / speed))

    def set_clip_speed_map(self, operation: dict[str, Any]) -> dict[str, Any]:
        track = self.find_track(operation.get("track"))
        _, _, entry, frame_in, frame_out = self._regular_clip(
            track, operation.get("item_index")
        )
        raw_keyframes = operation.get("keyframes")
        if not isinstance(raw_keyframes, list) or not 2 <= len(raw_keyframes) <= 64:
            raise ToolError("keyframes must contain between 2 and 64 points.")
        keyframes: list[tuple[int, float]] = []
        for index, raw in enumerate(raw_keyframes):
            if not isinstance(raw, dict):
                raise ToolError(f"keyframes[{index}] must be an object.")
            frame = _int(raw.get("frame"), f"keyframes[{index}].frame", 0)
            speed = _number(raw.get("speed"), f"keyframes[{index}].speed", 0.01)
            if speed > 100:
                raise ToolError(f"keyframes[{index}].speed must not exceed 100.")
            if keyframes and frame <= keyframes[-1][0]:
                raise ToolError("Speed-map frames must be strictly increasing.")
            keyframes.append((frame, speed))
        if keyframes[0][0] != 0:
            raise ToolError("The first speed-map keyframe must be at frame 0.")
        image_mode = operation.get("image_mode", "blend")
        if image_mode not in {"blend", "nearest"}:
            raise ToolError("image_mode must be blend or nearest.")
        pitch = _boolean(
            operation.get("pitch_compensation", True), "pitch_compensation"
        )
        service = self.isolate_entry_service(entry)
        if service.tag not in {"producer", "chain"}:
            raise ToolError("Speed maps require a producer or chain clip.")
        links = service.findall("link")
        timeremap_links = [
            link for link in links if _property(link, "mlt_service") == "timeremap"
        ]
        if len(timeremap_links) > 1 or (links and len(timeremap_links) != len(links)):
            raise ToolError("The clip contains an unowned or ambiguous chain link.")
        if _property(service, "mlt_service") == "timewarp":
            raise ToolError("Remove constant speed before applying a speed map.")
        if _property(service, "mlt_service") not in {
            "avformat",
            "avformat-novalidate",
            None,
        }:
            raise ToolError("Speed maps are supported only for ordinary media clips.")
        if service.tag == "producer":
            service.tag = "chain"
        if timeremap_links:
            link = timeremap_links[0]
        else:
            link = ET.Element("link", {"id": self.new_id("link")})
            first_filter = next(
                (index for index, child in enumerate(service) if child.tag == "filter"),
                len(service),
            )
            service.insert(first_filter, link)
            self.invalidate()
        serialized = ";".join(f"{frame}={speed:g}" for frame, speed in keyframes)
        _set_property(link, "mlt_service", "timeremap")
        _set_property(link, "speed_map", serialized)
        _set_property(link, "image_mode", image_mode)
        _set_property(link, "pitch", 1 if pitch else 0)
        source_frames = frame_out - frame_in + 1
        duration = self._speed_map_duration(source_frames, keyframes)
        if keyframes[-1][0] >= duration:
            raise ToolError(
                "A speed-map keyframe lies beyond the resulting clip duration."
            )
        entry.set("in", "0")
        entry.set("out", str(duration - 1))
        service.set("in", "0")
        service.set("out", str(duration - 1))
        _set_property(service, "length", duration)
        self.update_main_duration()
        return {
            "speed_map_updated": True,
            "duration_frames": duration,
            "keyframe_count": len(keyframes),
            "image_mode": image_mode,
            "pitch_compensation": pitch,
        }

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
        matches = [
            reference
            for reference in resource_references(self.root)
            if reference.decoded_value == old
            or (
                match_basename
                and Path(reference.decoded_value).name.casefold() == old.casefold()
            )
        ]
        if not matches:
            raise ToolError(f"No resource matches {old!r}.")
        if match_basename and len(matches) > 1 and not allow_multiple:
            raise ToolError(
                f"The basename {old!r} matches {len(matches)} resources; "
                "use the full path or allow_multiple=true."
            )
        normalized = str(new_path).replace("\\", "/")
        owners: dict[int, ET.Element] = {}
        for reference in matches:
            reference.replace_path(normalized)
            owners[id(reference.owner)] = reference.owner
        digest = shotcut_file_hash(new_path)
        for owner in owners.values():
            _set_property(owner, "shotcut:hash", digest)
            _set_property(owner, "shotcut:caption", new_path.name)
            for name in ("audio_index", "video_index", "astream", "vstream"):
                _remove_property(owner, name)
        return {
            "relinked": len(matches),
            "owners_updated": len(owners),
            "to": str(new_path),
            "shotcut_hash": digest,
        }

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
            "duplicate_item": self.duplicate_item,
            "replace_item_media": self.replace_item_media,
            "add_generator": self.add_generator,
            "remove_item": self.remove_item,
            "trim_item": self.trim_item,
            "roll_edit": self.roll_edit,
            "slip_item": self.slip_item,
            "slide_item": self.slide_item,
            "split_item": self.split_item,
            "move_item": self.move_item,
            "insert_gap": self.insert_gap,
            "remove_range": self.remove_range,
            "add_transition": self.add_transition,
            "remove_transition": self.remove_transition,
            "add_filter": self.add_filter,
            "update_filter": self.update_filter,
            "move_filter": self.move_filter,
            "remove_filter": self.remove_filter,
            "set_notes": self.set_notes,
            "add_marker": self.add_marker,
            "update_marker": self.update_marker,
            "remove_marker": self.remove_marker,
            "set_subtitle_track": self.set_subtitle_track,
            "remove_subtitle_track": self.remove_subtitle_track,
            "relink_media": self.relink_media,
            "set_profile": self.set_profile,
            "set_color_workflow": self.set_color_workflow,
            "set_clip_speed": self.set_clip_speed,
            "set_clip_speed_map": self.set_clip_speed_map,
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

    def to_bytes(self) -> bytes:
        ET.indent(self.tree, space="  ")
        return ET.tostring(self.root, encoding="utf-8", xml_declaration=True)
