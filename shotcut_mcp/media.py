"""Cached FFprobe inspection and stable media summaries."""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any

from .errors import ToolError
from .processes import (
    discover_executables,
    require_executable,
    run_capture,
    runtime_identity,
)

_PROBE_CACHE: dict[tuple[object, ...], dict[str, Any]] = {}
_PROBE_LOCK = threading.Lock()


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _fraction(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _as_float(value)
    numerator, denominator = value.split("/", 1)
    num, den = _as_float(numerator), _as_float(denominator)
    return num / den if num is not None and den not in (None, 0) else None


def media_duration(payload: dict[str, Any]) -> float | None:
    """Return the longest positive duration reported by FFprobe."""

    durations: list[float] = []
    value = _as_float(payload.get("format", {}).get("duration"))
    if value is not None and value > 0:
        durations.append(value)
    for stream in payload.get("streams", []):
        value = _as_float(stream.get("duration"))
        if value is not None and value > 0:
            durations.append(value)
    return max(durations) if durations else None


def probe_media_raw(media_path: Path) -> dict[str, Any]:
    """Return cached raw FFprobe JSON for a concrete file revision."""

    if not media_path.is_file():
        raise ToolError(f"Media file not found: {media_path}")
    stat = media_path.stat()
    ffprobe = require_executable(
        discover_executables().ffprobe, "ffprobe", "SHOTCUT_FFPROBE_PATH"
    )
    key = (
        str(media_path),
        stat.st_mtime_ns,
        stat.st_size,
        *runtime_identity(ffprobe),
    )
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    result = run_capture(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(media_path),
        ],
        timeout=60,
    )
    if result.returncode:
        raise ToolError(
            f"Failed to probe {media_path}: "
            f"{(result.stderr.strip() or 'unknown error')[-1200:]}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError("ffprobe returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ToolError("ffprobe returned an unexpected result.")
    with _PROBE_LOCK:
        if len(_PROBE_CACHE) > 256:
            _PROBE_CACHE.clear()
        _PROBE_CACHE[key] = payload
    return payload


def summarize_media(media_path: Path) -> dict[str, Any]:
    """Return the MCP-facing normalized summary for a media file."""

    payload = probe_media_raw(media_path)
    streams: list[dict[str, Any]] = []
    for stream in payload.get("streams", []):
        item: dict[str, Any] = {
            "index": stream.get("index"),
            "type": stream.get("codec_type"),
            "codec": stream.get("codec_name"),
            "duration_seconds": _as_float(stream.get("duration")),
        }
        if stream.get("codec_type") == "video":
            item.update(
                width=stream.get("width"),
                height=stream.get("height"),
                pixel_format=stream.get("pix_fmt"),
                frame_rate=_fraction(
                    stream.get("avg_frame_rate") or stream.get("r_frame_rate")
                ),
            )
        elif stream.get("codec_type") == "audio":
            item.update(
                sample_rate=_as_float(stream.get("sample_rate")),
                channels=stream.get("channels"),
                channel_layout=stream.get("channel_layout"),
            )
        streams.append(item)
    format_info = payload.get("format", {})
    return {
        "path": str(media_path),
        "size_bytes": media_path.stat().st_size,
        "duration_seconds": media_duration(payload),
        "format": format_info.get("format_long_name") or format_info.get("format_name"),
        "bit_rate": _as_float(format_info.get("bit_rate")),
        "streams": streams,
    }
