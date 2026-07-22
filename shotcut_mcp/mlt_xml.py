"""Shared decoding helpers for MLT XML primitive values."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET


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
