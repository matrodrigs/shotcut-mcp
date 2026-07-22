"""Shotcut executable discovery and safe subprocess integration."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ToolError


_PROBE_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_PROBE_LOCK = threading.Lock()
_SERVICE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_MELT_READY_CACHE: set[tuple[str, int, int]] = set()
_MELT_READY_LOCK = threading.Lock()


@dataclass(frozen=True)
class Executables:
    shotcut: Path | None
    melt: Path | None
    ffprobe: Path | None
    ffmpeg: Path | None


def expand_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("The path must be a non-empty string.")
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _which(name: str) -> Path | None:
    value = shutil.which(name)
    return Path(value).resolve() if value else None


def _first_existing(candidates: list[Path | None]) -> Path | None:
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def discover_executables() -> Executables:
    env_shotcut = os.environ.get("SHOTCUT_PATH")
    shotcut_candidates: list[Path | None] = [
        expand_path(env_shotcut) if env_shotcut else None,
        _which("shotcut"),
        _which("shotcut.exe"),
    ]
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
        shotcut_candidates.extend(
            [
                program_files / "Shotcut" / "shotcut.exe",
                local_appdata / "Programs" / "Shotcut" / "shotcut.exe",
            ]
        )
    elif sys_platform() == "darwin":
        shotcut_candidates.append(
            Path("/Applications/Shotcut.app/Contents/MacOS/shotcut")
        )
    shotcut = _first_existing(shotcut_candidates)
    sibling = shotcut.parent if shotcut else None

    def sibling_candidate(name: str) -> Path | None:
        return sibling / name if sibling else None

    melt_env = os.environ.get("SHOTCUT_MELT_PATH")
    ffprobe_env = os.environ.get("SHOTCUT_FFPROBE_PATH")
    ffmpeg_env = os.environ.get("SHOTCUT_FFMPEG_PATH")
    melt = _first_existing(
        [
            expand_path(melt_env) if melt_env else None,
            sibling_candidate("melt.exe" if os.name == "nt" else "melt"),
            _which("melt"),
            _which("shotcut.melt"),
            Path("/Applications/Shotcut.app/Contents/MacOS/melt")
            if sys_platform() == "darwin"
            else None,
        ]
    )
    ffprobe = _first_existing(
        [
            expand_path(ffprobe_env) if ffprobe_env else None,
            sibling_candidate("ffprobe.exe" if os.name == "nt" else "ffprobe"),
            _which("ffprobe"),
        ]
    )
    ffmpeg = _first_existing(
        [
            expand_path(ffmpeg_env) if ffmpeg_env else None,
            sibling_candidate("ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
            _which("ffmpeg"),
        ]
    )
    return Executables(shotcut=shotcut, melt=melt, ffprobe=ffprobe, ffmpeg=ffmpeg)


def sys_platform() -> str:
    import sys

    return sys.platform


def creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def require_executable(path: Path | None, label: str, env_name: str) -> Path:
    if path is None:
        raise ToolError(f"{label} was not found. Install Shotcut or set {env_name}.")
    return path


def run_capture(
    command: list[str], timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=creation_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"The command timed out after {timeout} seconds.") from exc
    except OSError as exc:
        raise ToolError(f"Could not run {command[0]}: {exc}") from exc


def ensure_melt_ready(melt: Path, *, attempts: int = 3, timeout: int = 5) -> None:
    """Warm MLT's module repository and tolerate one-time cold starts.

    A newly installed or extracted Windows build can spend long enough loading
    its DLL-backed modules that an ordinary validation command times out. A
    short terminated attempt warms the operating-system loader; retrying then
    completes normally with the full repository available. Cache readiness by
    executable identity so normal operations pay no repeated startup probe.
    """

    stat = melt.stat()
    cache_key = (str(melt), stat.st_mtime_ns, stat.st_size)
    with _MELT_READY_LOCK:
        if cache_key in _MELT_READY_CACHE:
            return

        last_timeout: ToolError | None = None
        command = [str(melt), "-query", "consumers"]
        for _ in range(attempts):
            try:
                result = run_capture(command, timeout=timeout)
            except ToolError as exc:
                if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
                    raise
                last_timeout = exc
                continue
            if result.returncode:
                detail = (result.stderr.strip() or result.stdout.strip())[-1200:]
                raise ToolError(
                    f"MLT repository initialization failed: {detail or 'unknown error'}"
                )
            if len(_MELT_READY_CACHE) > 16:
                _MELT_READY_CACHE.clear()
            _MELT_READY_CACHE.add(cache_key)
            return

        raise ToolError(
            f"MLT repository initialization timed out after {attempts} attempts."
        ) from last_timeout


def version_line(executable: Path | None, args: list[str]) -> str | None:
    if executable is None:
        return None
    result = run_capture([str(executable), *args], timeout=10)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return output.splitlines()[0] if output else None


def status() -> dict[str, Any]:
    executables = discover_executables()
    repository_ready = False
    repository_error = None
    if executables.melt is not None:
        try:
            ensure_melt_ready(executables.melt, attempts=2, timeout=4)
            repository_ready = True
        except ToolError as exc:
            repository_error = str(exc)
    return {
        "ready": all((executables.shotcut, executables.melt, executables.ffprobe))
        and repository_ready,
        "shotcut": {
            "found": executables.shotcut is not None,
            "path": str(executables.shotcut) if executables.shotcut else None,
        },
        "melt": {
            "found": executables.melt is not None,
            "path": str(executables.melt) if executables.melt else None,
            "version": version_line(executables.melt, ["--version"]),
            "repository_ready": repository_ready,
            "repository_error": repository_error,
        },
        "ffprobe": {
            "found": executables.ffprobe is not None,
            "path": str(executables.ffprobe) if executables.ffprobe else None,
            "version": version_line(executables.ffprobe, ["-version"]),
        },
        "ffmpeg": {
            "found": executables.ffmpeg is not None,
            "path": str(executables.ffmpeg) if executables.ffmpeg else None,
            "version": version_line(executables.ffmpeg, ["-version"]),
        },
        "environment_overrides": {
            key: os.environ.get(key)
            for key in (
                "SHOTCUT_PATH",
                "SHOTCUT_MELT_PATH",
                "SHOTCUT_FFPROBE_PATH",
                "SHOTCUT_FFMPEG_PATH",
            )
        },
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
    if not media_path.is_file():
        raise ToolError(f"Media file not found: {media_path}")
    stat = media_path.stat()
    key = (str(media_path), stat.st_mtime_ns, stat.st_size)
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    ffprobe = require_executable(
        discover_executables().ffprobe, "ffprobe", "SHOTCUT_FFPROBE_PATH"
    )
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
            f"Failed to probe {media_path}: {(result.stderr.strip() or 'unknown error')[-1200:]}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError("ffprobe returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ToolError("ffprobe returned an unexpected result.")
    with _PROBE_LOCK:
        _PROBE_CACHE.clear() if len(_PROBE_CACHE) > 256 else None
        _PROBE_CACHE[key] = payload
    return payload


def summarize_media(media_path: Path) -> dict[str, Any]:
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


def validate_project_file(project_path: Path, timeout: int = 30) -> dict[str, Any]:
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    result = run_capture(
        [
            str(melt),
            str(project_path),
            "in=0",
            "out=0",
            "-consumer",
            "null",
            "real_time=-1",
            "terminate_on_pause=1",
            "-silent",
        ],
        timeout=timeout,
    )
    diagnostic = "\n".join(
        part for part in (result.stdout, result.stderr) if part
    ).strip()
    return {
        "valid": result.returncode == 0,
        "return_code": result.returncode,
        "diagnostic": diagnostic[-4000:] or None,
    }


def list_services(kind: str) -> dict[str, Any]:
    if kind not in {"filter", "transition", "producer", "consumer"}:
        raise ToolError("kind must be filter, transition, producer, or consumer.")
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    cache_key = (str(melt), kind)
    if cache_key in _SERVICE_CACHE:
        return _SERVICE_CACHE[cache_key]
    result = run_capture([str(melt), "-query", f"{kind}s"], timeout=30)
    names = sorted(
        set(re.findall(r"^\s*-\s+([^\s#]+)\s*$", result.stdout, re.MULTILINE))
    )
    payload = {"kind": kind, "count": len(names), "services": names}
    _SERVICE_CACHE[cache_key] = payload
    return payload


def describe_service(kind: str, name: str) -> dict[str, Any]:
    if kind not in {"filter", "transition", "producer", "consumer"}:
        raise ToolError("kind must be filter, transition, producer, or consumer.")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:+-]+", name):
        raise ToolError("Invalid MLT service name.")
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    result = run_capture([str(melt), "-query", f"{kind}={name}"], timeout=30)
    text = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return {
        "kind": kind,
        "name": name,
        "available": result.returncode == 0 and bool(text),
        "metadata": text[-20000:] or None,
    }


def open_in_shotcut(path: Path, fullscreen: bool = False) -> dict[str, Any]:
    if not path.exists():
        raise ToolError(f"File or directory not found: {path}")
    shotcut = require_executable(
        discover_executables().shotcut, "Shotcut", "SHOTCUT_PATH"
    )
    command = [str(shotcut), *(["--fullscreen"] if fullscreen else []), str(path)]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags(),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise ToolError(f"Could not open Shotcut: {exc}") from exc
    return {"opened": True, "path": str(path), "pid": process.pid}


def render_preview(
    project_path: Path, output_path: Path, frame: int, overwrite: bool
) -> dict[str, Any]:
    if not project_path.is_file():
        raise ToolError(f"Project not found: {project_path}")
    if frame < 0:
        raise ToolError("frame must be zero or positive.")
    if output_path.exists() and not overwrite:
        raise ToolError(f"The image already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    result = run_capture(
        [
            str(melt),
            str(project_path),
            f"in={frame}",
            f"out={frame}",
            "-consumer",
            f"avformat:{output_path}",
            "f=image2",
            "vcodec=png",
            "real_time=-1",
            "terminate_on_pause=1",
            "-silent",
        ],
        timeout=120,
    )
    if result.returncode or not output_path.is_file():
        detail = "\n".join(
            part for part in (result.stdout, result.stderr) if part
        ).strip()
        raise ToolError(
            f"Failed to generate preview: {detail[-2000:] or 'output was not created'}"
        )
    return {
        "created": True,
        "path": str(output_path),
        "frame": frame,
        "size_bytes": output_path.stat().st_size,
    }
