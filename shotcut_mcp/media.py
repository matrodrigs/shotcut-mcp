"""Cached FFprobe inspection and stable media summaries."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import (
    Literal,
    SupportsFloat,
    SupportsIndex,
    SupportsInt,
    TypedDict,
    TypeGuard,
    TypeVar,
)

from .errors import ToolError
from .processes import (
    discover_executables,
    require_executable,
    run_capture,
    runtime_identity,
)
from .protocol import is_array, is_object, is_text_array, report_progress

ProbePayload = TypedDict(
    "ProbePayload",
    {
        "format": dict[str, object],
        "streams": list[dict[str, object]],
    },
    total=False,
)
ProbeField = TypeVar("ProbeField", str, int)


def _is_probe_payload(value: object) -> TypeGuard[ProbePayload]:
    if not is_object(value):
        return False
    streams = value.get("streams", [])
    return (
        is_object(value.get("format", {}))
        and is_array(streams)
        and all(is_object(stream) for stream in streams)
    )


def _probe_field(
    source: Mapping[str, object], name: str, kind: type[ProbeField]
) -> ProbeField | None:
    value = source.get(name)
    if value is None:
        return None
    if isinstance(value, kind) and not isinstance(value, bool):
        return value
    raise ToolError(
        "ffprobe returned an invalid field type.",
        code="media_probe_failed",
        recommended_action="run_compatibility_diagnostics",
        recommended_tool="shotcut_doctor",
        details={"field": name},
    )


# Raw FFprobe JSON is an extensible external payload. Keep its dynamic type at
# this boundary; normalized results below expose concrete field contracts.
_PROBE_CACHE: dict[tuple[object, ...], ProbePayload] = {}
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


_StreamIdentity = TypedDict(
    "_StreamIdentity",
    {
        "index": int | None,
        "type": str | None,
        "codec": str | None,
        "duration_seconds": float | None,
    },
)


# Video/audio fields are present only for the matching stream kind.
_MediaStreamSummaryFields = TypedDict(
    "_MediaStreamSummaryFields",
    {
        "width": int | None,
        "height": int | None,
        "pixel_format": str | None,
        "pixel_bit_depth": int | None,
        "color_primaries": str | None,
        "color_transfer": str | None,
        "color_space": str | None,
        "color_range": str | None,
        "dynamic_range": str,
        "frame_rate": float | None,
        "sample_rate": float | None,
        "channels": int | None,
        "channel_layout": str | None,
    },
    total=False,
)


class MediaStreamSummary(_StreamIdentity, _MediaStreamSummaryFields):
    pass


MediaSummary = TypedDict(
    "MediaSummary",
    {
        "path": str,
        "size_bytes": int,
        "duration_seconds": float | None,
        "format": str | None,
        "bit_rate": float | None,
        "streams": list[MediaStreamSummary],
    },
)


QualityAnalyzerCapability = TypedDict(
    "QualityAnalyzerCapability",
    {
        "filter": str,
        "stream_type": str,
        "available": bool,
        "error": str | None,
    },
)


QualityInterval = TypedDict(
    "QualityInterval",
    {
        "start_seconds": float | None,
        "end_seconds": float | None,
        "duration_seconds": float | None,
    },
)


QualityMetrics = TypedDict(
    "QualityMetrics",
    {
        "intervals": list[QualityInterval],
        "intervals_truncated": bool,
        "repeated_fields": dict[str, int],
        "single_frame_detection": dict[str, int],
        "multi_frame_detection": dict[str, int],
        "integrated_lufs": float | None,
        "loudness_range_lu": float | None,
        "lra_low_lufs": float | None,
        "lra_high_lufs": float | None,
        "true_peak_dbfs": float | None,
    },
    total=False,
)
_StreamAnalysisFields = TypedDict(
    "_StreamAnalysisFields",
    {
        "stream_index": int | None,
        "status": Literal["ok", "failed"],
        "error": str,
    },
    total=False,
)


class StreamAnalysis(QualityMetrics, _StreamAnalysisFields):
    pass


AnalyzerResult = TypedDict(
    "AnalyzerResult",
    {
        "status": Literal["ok", "partial", "failed", "unavailable", "not_applicable"],
        "filter": str,
        "streams": list[StreamAnalysis],
        "reason": str,
    },
    total=False,
)
QualityReport = TypedDict(
    "QualityReport",
    {
        "path": str,
        "media_duration_seconds": float | None,
        "start_seconds": float,
        "duration_seconds": float | None,
        "streams": dict[str, int | None],
        "analyzers": dict[str, AnalyzerResult],
        "requested_analyzers": list[str],
    },
)


def _as_float(value: object) -> float | None:
    if not isinstance(value, (str, bytes, bytearray, SupportsFloat, SupportsIndex)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _fraction(value: object) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _as_float(value)
    numerator, denominator = value.split("/", 1)
    num, den = _as_float(numerator), _as_float(denominator)
    return num / den if num is not None and den not in (None, 0) else None


def media_duration(payload: ProbePayload) -> float | None:
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


def _pixel_bit_depth(stream: dict[str, object]) -> int | None:
    raw = stream.get("bits_per_raw_sample")
    try:
        if raw not in (None, "", "0") and isinstance(
            raw, (str, bytes, bytearray, SupportsInt, SupportsIndex)
        ):
            return int(raw)
    except (TypeError, ValueError):
        pass
    pixel_format = str(stream.get("pix_fmt") or "")
    match = re.search(r"(?:p|le|be)(9|10|12|14|16)(?:le|be)?$", pixel_format)
    return int(match.group(1)) if match else 8 if pixel_format else None


def _dynamic_range(transfer: object) -> str:
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


def probe_media_raw(media_path: Path) -> ProbePayload:
    """Return cached raw FFprobe JSON for a concrete file revision."""

    if not media_path.is_file():
        raise ToolError(
            f"Media file not found: {media_path}",
            code="media_not_found",
            recommended_action="check_media_path_and_retry",
            details={"path": str(media_path)},
        )
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
            f"{(result.stderr.strip() or 'unknown error')[-1200:]}",
            code="media_probe_failed",
            recommended_action="inspect_media_or_run_compatibility_diagnostics",
            recommended_tool="shotcut_doctor",
            details={"path": str(media_path), "return_code": result.returncode},
        )
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(
            "ffprobe returned invalid JSON.",
            code="media_probe_failed",
            recommended_action="run_compatibility_diagnostics",
            recommended_tool="shotcut_doctor",
            details={"path": str(media_path)},
        ) from exc
    if not _is_probe_payload(payload):
        raise ToolError(
            "ffprobe returned an unexpected result.",
            code="media_probe_failed",
            recommended_action="run_compatibility_diagnostics",
            recommended_tool="shotcut_doctor",
            details={"path": str(media_path)},
        )
    with _PROBE_LOCK:
        if len(_PROBE_CACHE) > 256:
            _PROBE_CACHE.clear()
        _PROBE_CACHE[key] = payload
    return payload


def summarize_media(media_path: Path) -> MediaSummary:
    """Return the MCP-facing normalized summary for a media file."""

    payload = probe_media_raw(media_path)
    streams: list[MediaStreamSummary] = []
    for stream in payload.get("streams", []):
        item: MediaStreamSummary = {
            "index": _probe_field(stream, "index", int),
            "type": _probe_field(stream, "codec_type", str),
            "codec": _probe_field(stream, "codec_name", str),
            "duration_seconds": _as_float(stream.get("duration")),
        }
        if _probe_field(stream, "codec_type", str) == "video":
            item.update(
                {
                    "width": _probe_field(stream, "width", int),
                    "height": _probe_field(stream, "height", int),
                    "pixel_format": _probe_field(stream, "pix_fmt", str),
                    "pixel_bit_depth": _pixel_bit_depth(stream),
                    "color_primaries": _probe_field(stream, "color_primaries", str),
                    "color_transfer": _probe_field(stream, "color_transfer", str),
                    "color_space": _probe_field(stream, "color_space", str),
                    "color_range": _probe_field(stream, "color_range", str),
                    "dynamic_range": _dynamic_range(
                        _probe_field(stream, "color_transfer", str)
                    ),
                    "frame_rate": _fraction(
                        stream.get("avg_frame_rate") or stream.get("r_frame_rate")
                    ),
                }
            )
        elif _probe_field(stream, "codec_type", str) == "audio":
            item.update(
                {
                    "sample_rate": _as_float(stream.get("sample_rate")),
                    "channels": _probe_field(stream, "channels", int),
                    "channel_layout": _probe_field(stream, "channel_layout", str),
                }
            )
        streams.append(item)
    format_info = payload.get("format", {})
    return {
        "path": str(media_path),
        "size_bytes": media_path.stat().st_size,
        "duration_seconds": media_duration(payload),
        "format": _probe_field(format_info, "format_long_name", str)
        or _probe_field(format_info, "format_name", str),
        "bit_rate": _as_float(format_info.get("bit_rate")),
        "streams": streams,
    }


def _quality_number(
    value: object,
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
        diagnostic = (
            result.stderr.strip() or result.stdout.strip() or "unknown error"
        )[-1200:]
        raise ToolError(
            f"Could not query FFmpeg filters: {diagnostic}",
            code="ffmpeg_capability_query_failed",
            recommended_action="run_compatibility_diagnostics",
            recommended_tool="shotcut_doctor",
            details={
                "query": "filters",
                "ffmpeg_path": str(ffmpeg),
                "return_code": result.returncode,
                "diagnostic": diagnostic,
            },
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


def quality_analyzer_capabilities(
    ffmpeg: Path | None,
) -> dict[str, QualityAnalyzerCapability]:
    """Report whether each bounded quality analyzer is available in FFmpeg."""

    error: str | None = None
    available_filters: set[str] = set()
    if ffmpeg is None or not ffmpeg.is_file():
        error = "FFmpeg was not found."
    else:
        try:
            available_filters = _available_ffmpeg_filters(ffmpeg)
        except ToolError as exc:
            error = str(exc)
    return {
        name: {
            "filter": filter_name,
            "stream_type": stream_type,
            "available": filter_name in available_filters,
            "error": error,
        }
        for name, (filter_name, stream_type) in QUALITY_ANALYZERS.items()
    }


def _decimal(value: str) -> float | None:
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _bounded_intervals(
    intervals: list[QualityInterval], maximum: int
) -> tuple[list[QualityInterval], bool]:
    return intervals[:maximum], len(intervals) > maximum


def _parse_silence(text: str, offset: float, maximum: int) -> StreamAnalysis:
    intervals: list[QualityInterval] = []
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


def _parse_black(text: str, offset: float, maximum: int) -> StreamAnalysis:
    intervals: list[QualityInterval] = []
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


def _parse_freeze(text: str, offset: float, maximum: int) -> StreamAnalysis:
    intervals: list[QualityInterval] = []
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


def _parse_interlace(text: str) -> StreamAnalysis:
    result: dict[str, dict[str, int]] = {}
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
    metrics: StreamAnalysis = {}
    if "repeated_fields" in result:
        metrics["repeated_fields"] = result["repeated_fields"]
    if "single_frame_detection" in result:
        metrics["single_frame_detection"] = result["single_frame_detection"]
    if "multi_frame_detection" in result:
        metrics["multi_frame_detection"] = result["multi_frame_detection"]
    return metrics


def _last_metric(text: str, pattern: str) -> float | None:
    matches = re.findall(pattern, text, re.I)
    return _decimal(matches[-1]) if matches else None


def _parse_loudness(text: str) -> StreamAnalysis:
    summary = text.rsplit("Summary:", 1)[-1]
    return {
        "integrated_lufs": _last_metric(summary, r"\bI:\s*([-+0-9.eE]+)\s+LUFS"),
        "loudness_range_lu": _last_metric(summary, r"\bLRA:\s*([-+0-9.eE]+)\s+LU"),
        "lra_low_lufs": _last_metric(summary, r"LRA low:\s*([-+0-9.eE]+)\s+LUFS"),
        "lra_high_lufs": _last_metric(summary, r"LRA high:\s*([-+0-9.eE]+)\s+LUFS"),
        "true_peak_dbfs": _last_metric(summary, r"\bPeak:\s*([-+0-9.eE]+)\s+dBFS"),
    }


def _quality_filter(name: str, arguments: dict[str, object]) -> str:
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
    media_path: Path, arguments: dict[str, object]
) -> QualityReport:
    """Run bounded FFmpeg analyzers and return normalized machine-readable results."""

    if not media_path.is_file():
        raise ToolError(
            f"Media file not found: {media_path}",
            code="media_not_found",
            recommended_action="check_media_path_and_retry",
            details={"path": str(media_path)},
        )
    requested = arguments.get("analyzers", list(QUALITY_ANALYZERS))
    if (
        not is_text_array(requested)
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
    streams_by_type: dict[str, list[dict[str, object]]] = {}
    for kind in ("audio", "video"):
        available_streams = [
            stream
            for stream in payload.get("streams", [])
            if is_object(stream) and stream.get("codec_type") == kind
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
                    f"{selector_name}={selected_index} does not identify a {kind} stream.",
                    code="media_stream_not_found",
                    recommended_action="probe_media_and_choose_stream",
                    recommended_tool="probe_media",
                    details={
                        "path": str(media_path),
                        "selector": selector_name,
                        "selected_index": selected_index,
                        "stream_type": kind,
                        "available_stream_indices": [
                            stream.get("index") for stream in available_streams
                        ],
                    },
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
    results: dict[str, AnalyzerResult] = {}
    parsers: dict[str, Callable[[str], StreamAnalysis]] = {
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
        stream_results: list[StreamAnalysis] = []
        for stream in streams:
            stream_index = _probe_field(stream, "index", int)
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
            item: StreamAnalysis
            if result.returncode:
                item = {
                    "stream_index": stream_index,
                    "status": "failed",
                    "error": (text.strip() or "FFmpeg failed")[-1200:],
                }
            else:
                item = parsers[name](text)
                item["stream_index"] = stream_index
                item["status"] = "ok"
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
                _probe_field(streams_by_type["audio"][0], "index", int)
                if streams_by_type["audio"]
                else None
            ),
            "video_stream_index": (
                _probe_field(streams_by_type["video"][0], "index", int)
                if streams_by_type["video"]
                else None
            ),
        },
        "analyzers": results,
        "requested_analyzers": analyzers,
    }
