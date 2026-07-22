"""Shared decoding helpers for MLT XML primitive values."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

RESOURCE_PROPERTY_NAMES = frozenset(
    {
        "av.file",
        "composite.luma",
        "filename",
        "luma",
        "luma.resource",
        "producer.resource",
        "resource",
        "shotcut:originalResource",
        "shotcut:proxy",
        "shotcut:proxyResource",
        "src",
        "warp_resource",
    }
)
_SPEED_PREFIX = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+):)(.+)$", re.S)
_NON_FILE_RESOURCES = ("color:", "colour:", "noise:", "tone:")


@dataclass
class ResourceReference:
    """One path-bearing MLT value with enough context for safe replacement."""

    owner: ET.Element
    name: str
    stored_value: str
    decoded_value: str
    prefix: str = ""
    property_element: ET.Element | None = None
    attribute_name: str | None = None

    @property
    def owner_id(self) -> str | None:
        return self.owner.get("id")

    @property
    def owner_tag(self) -> str:
        return self.owner.tag

    def replace_path(self, value: str) -> None:
        """Replace only the decoded path while preserving an MLT speed prefix."""

        stored = f"{self.prefix}{value}"
        if self.property_element is not None:
            self.property_element.text = stored
        elif self.attribute_name is not None:
            self.owner.set(self.attribute_name, stored)
        self.stored_value = stored
        self.decoded_value = value


def decode_resource_value(name: str, value: str) -> tuple[str, str] | None:
    """Return ``(path, prefix)`` for a path-bearing MLT value."""

    cleaned = value.strip()
    if not cleaned or cleaned.lower().startswith(_NON_FILE_RESOURCES):
        return None
    if name in {"resource", "producer.resource"}:
        match = _SPEED_PREFIX.match(cleaned)
        if match:
            return match.group(2), match.group(1)
    return cleaned, ""


def resource_references(root: ET.Element) -> list[ResourceReference]:
    """Decode every resource representation owned by the supported MLT format."""

    references: list[ResourceReference] = []
    for owner in root.iter():
        owner_service = property_value(owner, "mlt_service")
        for prop in owner.findall("property"):
            name = prop.get("name", "")
            if name not in RESOURCE_PROPERTY_NAMES:
                continue
            stored = prop.text or ""
            if (
                name == "resource"
                and owner_service in {"color", "colour"}
                and not any(marker in stored for marker in ("/", "\\", ":"))
            ):
                continue
            decoded = decode_resource_value(name, stored)
            if decoded is None:
                continue
            value, prefix = decoded
            references.append(
                ResourceReference(
                    owner=owner,
                    name=name,
                    stored_value=stored,
                    decoded_value=value,
                    prefix=prefix,
                    property_element=prop,
                )
            )
        stored_attribute = owner.get("resource")
        if stored_attribute:
            decoded = decode_resource_value("resource", stored_attribute)
            if decoded is not None:
                value, prefix = decoded
                references.append(
                    ResourceReference(
                        owner=owner,
                        name="resource",
                        stored_value=stored_attribute,
                        decoded_value=value,
                        prefix=prefix,
                        attribute_name="resource",
                    )
                )
    return references


def property_value(element: ET.Element, name: str) -> str | None:
    """Read one direct MLT property without collapsing an empty value to missing."""

    for prop in element.findall("property"):
        if prop.get("name") == name:
            return prop.text or ""
    return None


def properties(element: ET.Element) -> dict[str, str]:
    """Read all named direct MLT properties from an element."""

    return {
        prop.get("name", ""): prop.text or ""
        for prop in element.findall("property")
        if prop.get("name")
    }


def clock_to_frames(value: str | None, fps: float) -> int | None:
    """Decode an MLT frame integer or clock string into a frame number."""

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
