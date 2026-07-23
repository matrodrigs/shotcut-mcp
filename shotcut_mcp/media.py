"""Cached FFprobe inspection and stable media summaries."""

from __future__ import annotations

import hashlib
import json
import math
import re
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
from .protocol import report_progress

_PROBE_CACHE: dict[tuple[object, ...], dict[str, Any]] = {}
_PROBE_LOCK = threading.Lock()
_FILTER_CACHE: dict[tuple[object, ...], set[str]] = {}
_FILTER_LOCK = threading.Lock()

QUALITY_ANALYZERS = {
    "silence": ("silencedetect", "audio"),
    "black": ("blackdetect", "video"),
    "freeze": ("freezedetect", "video"),
    "interlace": ("idet", "video"),
    "loudness": ("ebur128", "audio"),
}


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


def shotcut_file_hash(media_path: Path) -> str:
    """Return Shotcut's small-file or first/last-megabyte MD5 identity hash."""

    size = media_path.stat().st_size
    digest = hashlib.md5(usedforsecurity=False)
    with media_path.open("rb") as handle:
        if size < 2 * 1024 * 1024:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        else:
            digest.update(handle.read(1024 * 1024))
            handle.seek(-1024 * 1024, 2)
            digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def _pixel_bit_depth(stream: dict[str, Any]) -> int | None:
    raw = stream.get("bits_per_raw_sample")
    try:
        if raw not in (None, "", "0"):
            return int(raw)
    except (TypeError, ValueError):
        pass
    pixel_format = str(stream.get("pix_fmt") or "")
    match = re.search(r"(?:p|le|be)(9|10|12|14|16)(?:le|be)?$", pixel_format)
    return int(match.group(1)) if match else 8 if pixel_format else None


def _dynamic_range(transfer: Any) -> str:
    normalized = str(transfer or "").lower()
    if normalized in {"arib-std-b67", "hlg"}:
        return "hlg"
    if normalized in {"smpte2084", "pq"}:
        return "pq"
    if normalized in {
        "bt709",
        "bt470bg",
        "gamma22",
        "gamma28",
        "iec61966-2-1",
        "smpte170m",
    }:
        return "sdr"
    return "unknown"


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
                pixel_bit_depth=_pixel_bit_depth(stream),
                color_primaries=stream.get("color_primaries"),
                color_transfer=stream.get("color_transfer"),
                color_space=stream.get("color_space"),
                color_range=stream.get("color_range"),
                dynamic_range=_dynamic_range(stream.get("color_transfer")),
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


def _quality_number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    result = _as_float(value)
    if result is None or result < minimum or (maximum is not None and result > maximum):
        if maximum is None:
            raise ToolError(f"{name} must be at least {minimum}.")
        raise ToolError(f"{name} must be between {minimum} and {maximum}.")
    return result


def _available_ffmpeg_filters(ffmpeg: Path) -> set[str]:
    key = runtime_identity(ffmpeg)
    with _FILTER_LOCK:
        cached = _FILTER_CACHE.get(key)
    if cached is not None:
        return cached
    result = run_capture(
        [str(ffmpeg), "-hide_banner", "-filters"],
        timeout=30,
        max_output_bytes=2 * 1024 * 1024,
    )
    if result.returncode:
        raise ToolError(
            "Could not query FFmpeg filters: "
            + (result.stderr.strip() or result.stdout.strip() or "unknown error")[
                -1200:
            ]
        )
    filters = {
        match.group(1)
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if (match := re.match(r"^\s*[.A-Z|]{2,6}\s+([A-Za-z0-9_]+)\s", line))
    }
    with _FILTER_LOCK:
        if len(_FILTER_CACHE) > 16:
            _FILTER_CACHE.clear()
        _FILTER_CACHE[key] = filters
    return filters


def _decimal(value: str) -> float | None:
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _bounded_intervals(
    intervals: list[dict[str, Any]], maximum: int
) -> tuple[list[dict[str, Any]], bool]:
    return intervals[:maximum], len(intervals) > maximum


def _parse_silence(text: str, offset: float, maximum: int) -> dict[str, Any]:
    intervals: list[dict[str, Any]] = []
    pending: list[float] = []
    for line in text.splitlines():
        start_match = re.search(r"silence_start(?:\.\d+)?:\s*([-+0-9.eE]+)", line)
        if start_match:
            value = _decimal(start_match.group(1))
            if value is not None:
                pending.append(value + offset)
        end_match = re.search(
            r"silence_end(?:\.\d+)?:\s*([-+0-9.eE]+)"
            r"(?:\s*\|\s*silence_duration(?:\.\d+)?:\s*([-+0-9.eE]+))?",
            line,
        )
        if end_match:
            end = _decimal(end_match.group(1))
            duration = _decimal(end_match.group(2)) if end_match.group(2) else None
            if end is None:
                continue
            start = pending.pop(0) if pending else end + offset - (duration or 0.0)
            intervals.append(
                {
                    "start_seconds": start,
                    "end_seconds": end + offset,
                    "duration_seconds": duration,
                }
            )
    intervals.extend(
        {
            "start_seconds": start,
            "end_seconds": None,
            "duration_seconds": None,
        }
        for start in pending
    )
    shown, truncated = _bounded_intervals(intervals, maximum)
    return {"intervals": shown, "intervals_truncated": truncated}


def _parse_black(text: str, offset: float, maximum: int) -> dict[str, Any]:
    intervals = []
    for match in re.finditer(
        r"black_start:([-+0-9.eE]+)\s+black_end:([-+0-9.eE]+)\s+"
        r"black_duration:([-+0-9.eE]+)",
        text,
    ):
        start, end, duration = (_decimal(value) for value in match.groups())
        if start is not None and end is not None:
            intervals.append(
                {
                    "start_seconds": start + offset,
                    "end_seconds": end + offset,
                    "duration_seconds": duration,
                }
            )
    shown, truncated = _bounded_intervals(intervals, maximum)
    return {"intervals": shown, "intervals_truncated": truncated}


def _parse_freeze(text: str, offset: float, maximum: int) -> dict[str, Any]:
    intervals: list[dict[str, Any]] = []
    start: float | None = None
    duration: float | None = None
    for line in text.splitlines():
        start_match = re.search(r"freeze_start:\s*([-+0-9.eE]+)", line)
        if start_match:
            value = _decimal(start_match.group(1))
            start = value + offset if value is not None else None
        duration_match = re.search(r"freeze_duration:\s*([-+0-9.eE]+)", line)
        if duration_match:
            duration = _decimal(duration_match.group(1))
        end_match = re.search(r"freeze_end:\s*([-+0-9.eE]+)", line)
        if end_match:
            end = _decimal(end_match.group(1))
            if end is not None:
                resolved_start = start
                if resolved_start is None and duration is not None:
                    resolved_start = end + offset - duration
                intervals.append(
                    {
                        "start_seconds": resolved_start,
                        "end_seconds": end + offset,
                        "duration_seconds": duration,
                    }
                )
            start = None
            duration = None
    if start is not None:
        intervals.append(
            {"start_seconds": start, "end_seconds": None, "duration_seconds": duration}
        )
    shown, truncated = _bounded_intervals(intervals, maximum)
    return {"intervals": shown, "intervals_truncated": truncated}


def _parse_interlace(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    patterns = {
        "repeated_fields": (
            r"Repeated Fields:\s+Neither:\s*(\d+)\s+Top:\s*(\d+)\s+Bottom:\s*(\d+)",
            ("neither", "top", "bottom"),
        ),
        "single_frame_detection": (
            r"Single frame detection:\s+TFF:\s*(\d+)\s+BFF:\s*(\d+)\s+"
            r"Progressive:\s*(\d+)\s+Undetermined:\s*(\d+)",
            ("tff", "bff", "progressive", "undetermined"),
        ),
        "multi_frame_detection": (
            r"Multi frame detection:\s+TFF:\s*(\d+)\s+BFF:\s*(\d+)\s+"
            r"Progressive:\s*(\d+)\s+Undetermined:\s*(\d+)",
            ("tff", "bff", "progressive", "undetermined"),
        ),
    }
    for key, (pattern, labels) in patterns.items():
        matches = re.findall(pattern, text, re.I)
        if matches:
            result[key] = {
                label: int(value)
                for label, value in zip(labels, matches[-1], strict=True)
            }
    return result


def _last_metric(text: str, pattern: str) -> float | None:
    matches = re.findall(pattern, text, re.I)
    return _decimal(matches[-1]) if matches else None


def _parse_loudness(text: str) -> dict[str, Any]:
    summary = text.rsplit("Summary:", 1)[-1]
    return {
        "integrated_lufs": _last_metric(summary, r"\bI:\s*([-+0-9.eE]+)\s+LUFS"),
        "loudness_range_lu": _last_metric(summary, r"\bLRA:\s*([-+0-9.eE]+)\s+LU"),
        "lra_low_lufs": _last_metric(summary, r"LRA low:\s*([-+0-9.eE]+)\s+LUFS"),
        "lra_high_lufs": _last_metric(summary, r"LRA high:\s*([-+0-9.eE]+)\s+LUFS"),
        "true_peak_dbfs": _last_metric(summary, r"\bPeak:\s*([-+0-9.eE]+)\s+dBFS"),
    }


def _quality_filter(name: str, arguments: dict[str, Any]) -> str:
    if name == "silence":
        threshold = _quality_number(
            arguments.get("silence_threshold_db", -60),
            "silence_threshold_db",
            minimum=-120,
            maximum=0,
        )
        duration = _quality_number(
            arguments.get("silence_min_duration_seconds", 2),
            "silence_min_duration_seconds",
            minimum=0.05,
            maximum=3600,
        )
        return f"asetpts=PTS-STARTPTS,silencedetect=n={threshold:g}dB:d={duration:g}"
    if name == "black":
        duration = _quality_number(
            arguments.get("black_min_duration_seconds", 2),
            "black_min_duration_seconds",
            minimum=0.05,
            maximum=3600,
        )
        pixel = _quality_number(
            arguments.get("black_pixel_threshold", 0.1),
            "black_pixel_threshold",
            minimum=0,
            maximum=1,
        )
        picture = _quality_number(
            arguments.get("black_picture_threshold", 0.98),
            "black_picture_threshold",
            minimum=0,
            maximum=1,
        )
        return (
            "setpts=PTS-STARTPTS,"
            f"blackdetect=d={duration:g}:pix_th={pixel:g}:pic_th={picture:g}"
        )
    if name == "freeze":
        duration = _quality_number(
            arguments.get("freeze_min_duration_seconds", 2),
            "freeze_min_duration_seconds",
            minimum=0.05,
            maximum=3600,
        )
        noise = _quality_number(
            arguments.get("freeze_noise_db", -60),
            "freeze_noise_db",
            minimum=-120,
            maximum=0,
        )
        return f"setpts=PTS-STARTPTS,freezedetect=n={noise:g}dB:d={duration:g}"
    if name == "interlace":
        return "idet"
    dual_mono = arguments.get("dual_mono", False)
    if not isinstance(dual_mono, bool):
        raise ToolError("dual_mono must be a boolean.")
    return "ebur128=peak=true:framelog=verbose:dualmono=" + (
        "true" if dual_mono else "false"
    )


def analyze_media_quality(
    media_path: Path, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Run bounded FFmpeg analyzers and return normalized machine-readable results."""

    if not media_path.is_file():
        raise ToolError(f"Media file not found: {media_path}")
    requested = arguments.get("analyzers", list(QUALITY_ANALYZERS))
    if (
        not isinstance(requested, list)
        or not requested
        or len(requested) > len(QUALITY_ANALYZERS)
        or any(
            not isinstance(item, str) or item not in QUALITY_ANALYZERS
            for item in requested
        )
    ):
        raise ToolError(
            "analyzers must be a non-empty subset of: " + ", ".join(QUALITY_ANALYZERS)
        )
    analyzers = list(dict.fromkeys(requested))
    start = _quality_number(
        arguments.get("start_seconds", 0), "start_seconds", minimum=0
    )
    duration_value = arguments.get("duration_seconds")
    duration = (
        _quality_number(duration_value, "duration_seconds", minimum=0.001)
        if duration_value is not None
        else None
    )
    timeout = int(
        _quality_number(
            arguments.get("timeout_seconds", 300),
            "timeout_seconds",
            minimum=1,
            maximum=3600,
        )
    )
    maximum_intervals = int(
        _quality_number(
            arguments.get("max_intervals", 256),
            "max_intervals",
            minimum=1,
            maximum=1000,
        )
    )
    payload = probe_media_raw(media_path)
    streams_by_type: dict[str, list[dict[str, Any]]] = {}
    for kind in ("audio", "video"):
        available_streams = [
            stream
            for stream in payload.get("streams", [])
            if isinstance(stream, dict) and stream.get("codec_type") == kind
        ]
        selector_name = f"{kind}_stream_index"
        selected_index = arguments.get(selector_name)
        if selected_index is not None:
            if isinstance(selected_index, bool) or not isinstance(selected_index, int):
                raise ToolError(f"{selector_name} must be an integer.")
            matches = [
                stream
                for stream in available_streams
                if stream.get("index") == selected_index
            ]
            if len(matches) != 1:
                raise ToolError(
                    f"{selector_name}={selected_index} does not identify a {kind} stream."
                )
            streams_by_type[kind] = matches
        else:
            streams_by_type[kind] = available_streams[:1]
    ffmpeg = require_executable(
        discover_executables().ffmpeg, "ffmpeg", "SHOTCUT_FFMPEG_PATH"
    )
    available = _available_ffmpeg_filters(ffmpeg)
    invocation_count = sum(
        len(streams_by_type[kind])
        for name in analyzers
        for filter_name, kind in [QUALITY_ANALYZERS[name]]
        if filter_name in available
    )
    total = max(1, invocation_count)
    completed = 0
    report_progress(0, total, "Starting media quality analysis.")
    results: dict[str, Any] = {}
    parsers = {
        "silence": lambda text: _parse_silence(text, start, maximum_intervals),
        "black": lambda text: _parse_black(text, start, maximum_intervals),
        "freeze": lambda text: _parse_freeze(text, start, maximum_intervals),
        "interlace": _parse_interlace,
        "loudness": _parse_loudness,
    }
    for name in analyzers:
        filter_name, kind = QUALITY_ANALYZERS[name]
        streams = streams_by_type[kind]
        if filter_name not in available:
            results[name] = {
                "status": "unavailable",
                "filter": filter_name,
                "streams": [],
                "reason": f"FFmpeg filter {filter_name} is not installed.",
            }
            continue
        if not streams:
            results[name] = {
                "status": "not_applicable",
                "filter": filter_name,
                "streams": [],
                "reason": f"The media has no {kind} stream.",
            }
            continue
        stream_results: list[dict[str, Any]] = []
        for stream in streams:
            stream_index = stream.get("index")
            command = [
                str(ffmpeg),
                "-hide_banner",
                "-nostats",
                "-nostdin",
                "-v",
                "info",
            ]
            if start > 0:
                command.extend(["-ss", f"{start:g}"])
            command.extend(["-i", str(media_path)])
            if duration is not None:
                command.extend(["-t", f"{duration:g}"])
            command.extend(["-map", f"0:{stream_index}"])
            command.extend(["-vn", "-af"] if kind == "audio" else ["-an", "-vf"])
            command.extend([_quality_filter(name, arguments), "-f", "null", "-"])
            result = run_capture(
                command,
                timeout=timeout,
                max_output_bytes=4 * 1024 * 1024,
            )
            text = result.stderr + "\n" + result.stdout
            item: dict[str, Any] = {"stream_index": stream_index}
            if result.returncode:
                item.update(
                    status="failed", error=(text.strip() or "FFmpeg failed")[-1200:]
                )
            else:
                item.update(status="ok", **parsers[name](text))
            stream_results.append(item)
            completed += 1
            report_progress(
                completed,
                total,
                f"Completed {name} analysis for stream {stream_index}.",
            )
        statuses = {item["status"] for item in stream_results}
        results[name] = {
            "status": "ok"
            if statuses == {"ok"}
            else "partial"
            if "ok" in statuses
            else "failed",
            "filter": filter_name,
            "streams": stream_results,
        }
    if completed == 0:
        report_progress(total, total, "No applicable installed analyzers were run.")
    return {
        "path": str(media_path),
        "media_duration_seconds": media_duration(payload),
        "start_seconds": start,
        "duration_seconds": duration,
        "streams": {
            "audio_stream_index": (
                streams_by_type["audio"][0].get("index")
                if streams_by_type["audio"]
                else None
            ),
            "video_stream_index": (
                streams_by_type["video"][0].get("index")
                if streams_by_type["video"]
                else None
            ),
        },
        "analyzers": results,
        "requested_analyzers": analyzers,
    }
