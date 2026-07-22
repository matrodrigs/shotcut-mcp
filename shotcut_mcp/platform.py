"""Shotcut executable discovery and safe subprocess integration."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RequestCancelled, ToolError
from .protocol import cancellation_requested
from .storage import OutputTransaction

_PROBE_CACHE: dict[tuple[object, ...], dict[str, Any]] = {}
_PROBE_LOCK = threading.Lock()
_SERVICE_CACHE: dict[tuple[object, ...], dict[str, Any]] = {}
_SERVICE_LOCK = threading.Lock()
_MELT_READY_CACHE: set[tuple[object, ...]] = set()
_MELT_READY_LOCK = threading.Lock()
MLT_ENVIRONMENT_KEYS = (
    "MLT_DATA",
    "MLT_PRESETS_PATH",
    "MLT_PROFILES_PATH",
    "MLT_REPOSITORY",
    "MLT_REPOSITORY_DENY",
)


@dataclass(frozen=True)
class Executables:
    shotcut: Path | None
    melt: Path | None
    ffprobe: Path | None
    ffmpeg: Path | None


def expand_path(value: str, *, enforce_policy: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("The path must be a non-empty string.")
    expanded = Path(os.path.expandvars(value)).expanduser()
    if enforce_policy and (
        os.environ.get("SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS", "").lower()
        in {"1", "true", "yes"}
        and not expanded.is_absolute()
    ):
        raise ToolError(
            "Relative paths are disabled by SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS."
        )
    resolved = expanded.resolve()
    configured_roots = os.environ.get("SHOTCUT_MCP_ALLOWED_ROOTS", "").strip()
    if enforce_policy and configured_roots:
        roots = [
            Path(os.path.expandvars(item)).expanduser().resolve()
            for item in configured_roots.split(os.pathsep)
            if item.strip()
        ]
        candidate = os.path.normcase(str(resolved))
        allowed = False
        for root in roots:
            try:
                allowed = os.path.commonpath(
                    [candidate, os.path.normcase(str(root))]
                ) == os.path.normcase(str(root))
            except ValueError:
                allowed = False
            if allowed:
                break
        if not allowed:
            raise ToolError(
                f"Path is outside SHOTCUT_MCP_ALLOWED_ROOTS allowed roots: {resolved}"
            )
    return resolved


def path_policy() -> dict[str, Any]:
    configured = os.environ.get("SHOTCUT_MCP_ALLOWED_ROOTS", "").strip()
    return {
        "allowed_roots": [item for item in configured.split(os.pathsep) if item]
        if configured
        else None,
        "require_absolute_paths": os.environ.get(
            "SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS", ""
        ).lower()
        in {"1", "true", "yes"},
        "unsafe_consumer_properties": os.environ.get(
            "SHOTCUT_MCP_ALLOW_UNSAFE_CONSUMER_PROPERTIES", ""
        ).lower()
        in {"1", "true", "yes"},
        "allow_network_resources": os.environ.get(
            "SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES", ""
        ).lower()
        in {"1", "true", "yes"},
    }


def is_network_resource(value: str) -> bool:
    network_schemes = {
        "ftp",
        "ftps",
        "http",
        "https",
        "nfs",
        "rtmp",
        "rtp",
        "rtsp",
        "sftp",
        "smb",
        "srt",
        "tcp",
        "udp",
    }
    return (
        value.startswith(("//", "\\\\"))
        or value.partition(":")[0].lower() in network_schemes
    )


def project_network_resources(project_path: Path) -> list[str]:
    try:
        root = ET.parse(project_path).getroot()
    except (ET.ParseError, OSError):
        return []
    values = [
        (element.text or "").strip()
        for element in root.findall(".//property[@name='resource']")
    ]
    values.extend(
        value.strip() for element in root.iter() if (value := element.get("resource"))
    )
    return sorted({value for value in values if is_network_resource(value)})


def enforce_project_resource_policy(project_path: Path) -> None:
    if os.environ.get("SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return
    resources = project_network_resources(project_path)
    if resources:
        preview = ", ".join(resources[:3])
        raise ToolError(
            "Project network resources are disabled by default: "
            f"{preview}. An administrator can opt in with "
            "SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES=1."
        )


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
        expand_path(env_shotcut, enforce_policy=False) if env_shotcut else None,
        _which("shotcut"),
        _which("shotcut.exe"),
    ]
    if os.name == "nt":
        program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
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
            expand_path(melt_env, enforce_policy=False) if melt_env else None,
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
            expand_path(ffprobe_env, enforce_policy=False) if ffprobe_env else None,
            sibling_candidate("ffprobe.exe" if os.name == "nt" else "ffprobe"),
            _which("ffprobe"),
        ]
    )
    ffmpeg = _first_existing(
        [
            expand_path(ffmpeg_env, enforce_policy=False) if ffmpeg_env else None,
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


def _runtime_identity(executable: Path) -> tuple[object, ...]:
    try:
        stat = executable.stat()
    except OSError as exc:
        raise ToolError(f"Could not inspect executable {executable}: {exc}") from exc
    environment = tuple((key, os.environ.get(key)) for key in MLT_ENVIRONMENT_KEYS)
    return (str(executable), stat.st_mtime_ns, stat.st_size, environment)


def run_capture(
    command: list[str], timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    if cancellation_requested():
        raise RequestCancelled("Request cancelled before the command started.")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags(),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise ToolError(f"Could not run {command[0]}: {exc}") from exc
    deadline = time.monotonic() + timeout
    while True:
        if cancellation_requested():
            terminate_process(process)
            raise RequestCancelled("Request cancelled by the MCP client.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_process(process)
            expired = subprocess.TimeoutExpired(command, timeout)
            raise ToolError(
                f"The command timed out after {timeout} seconds."
            ) from expired
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            return subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr
            )
        except subprocess.TimeoutExpired:
            continue


def terminate_process(process: subprocess.Popen[Any], grace_seconds: float = 2) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            kill_group = getattr(os, "killpg", None)
            if not callable(kill_group):
                raise OSError("process-group signals are unavailable")
            kill_group(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name != "nt":
                kill_group = getattr(os, "killpg", None)
                if not callable(kill_group):
                    raise OSError("process-group signals are unavailable")
                kill_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            else:
                process.kill()
            process.wait(timeout=grace_seconds)
        except (OSError, subprocess.TimeoutExpired):
            pass


def ensure_melt_ready(melt: Path, *, attempts: int = 3, timeout: int = 5) -> None:
    """Warm MLT's module repository and tolerate one-time cold starts.

    A newly installed or extracted Windows build can spend long enough loading
    its DLL-backed modules that an ordinary validation command times out. A
    short terminated attempt warms the operating-system loader; retrying then
    completes normally with the full repository available. Cache readiness by
    executable identity so normal operations pay no repeated startup probe.
    """

    cache_key = _runtime_identity(melt)
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


def _safe_version(
    executable: Path | None, args: list[str]
) -> tuple[str | None, str | None]:
    try:
        return version_line(executable, args), None
    except ToolError as exc:
        return None, str(exc)


def status() -> dict[str, Any]:
    executables = discover_executables()
    shotcut_version, shotcut_version_error = _safe_version(
        executables.shotcut, ["--version"]
    )
    melt_version, melt_version_error = _safe_version(executables.melt, ["--version"])
    ffprobe_version, ffprobe_version_error = _safe_version(
        executables.ffprobe, ["-version"]
    )
    ffmpeg_version, ffmpeg_version_error = _safe_version(
        executables.ffmpeg, ["-version"]
    )
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
            "version": shotcut_version,
            "version_error": shotcut_version_error,
        },
        "melt": {
            "found": executables.melt is not None,
            "path": str(executables.melt) if executables.melt else None,
            "version": melt_version,
            "version_error": melt_version_error,
            "repository_ready": repository_ready,
            "repository_error": repository_error,
        },
        "ffprobe": {
            "found": executables.ffprobe is not None,
            "path": str(executables.ffprobe) if executables.ffprobe else None,
            "version": ffprobe_version,
            "version_error": ffprobe_version_error,
        },
        "ffmpeg": {
            "found": executables.ffmpeg is not None,
            "path": str(executables.ffmpeg) if executables.ffmpeg else None,
            "version": ffmpeg_version,
            "version_error": ffmpeg_version_error,
        },
        "environment_overrides": {
            key: os.environ.get(key)
            for key in (
                "SHOTCUT_PATH",
                "SHOTCUT_MELT_PATH",
                "SHOTCUT_FFPROBE_PATH",
                "SHOTCUT_FFMPEG_PATH",
                "SHOTCUT_MCP_ALLOWED_ROOTS",
                "SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS",
                "SHOTCUT_MCP_ALLOW_UNSAFE_CONSUMER_PROPERTIES",
                "SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES",
                *MLT_ENVIRONMENT_KEYS,
            )
        },
        "path_policy": path_policy(),
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
    ffprobe = require_executable(
        discover_executables().ffprobe, "ffprobe", "SHOTCUT_FFPROBE_PATH"
    )
    key = (
        str(media_path),
        stat.st_mtime_ns,
        stat.st_size,
        *_runtime_identity(ffprobe),
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
    enforce_project_resource_policy(project_path)
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
    if kind not in {"filter", "transition", "producer", "consumer", "link"}:
        raise ToolError("kind must be filter, transition, producer, consumer, or link.")
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    cache_key = (*_runtime_identity(melt), kind)
    with _SERVICE_LOCK:
        cached = _SERVICE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result = run_capture([str(melt), "-query", f"{kind}s"], timeout=30)
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip())[-1200:]
        raise ToolError(f"MLT service query failed: {detail or result.returncode}")
    names = sorted(
        set(re.findall(r"^\s*-\s+([^\s#]+)\s*$", result.stdout, re.MULTILINE))
    )
    payload = {"kind": kind, "count": len(names), "services": names}
    with _SERVICE_LOCK:
        if len(_SERVICE_CACHE) > 64:
            _SERVICE_CACHE.clear()
        _SERVICE_CACHE[cache_key] = payload
    return payload


def describe_service(kind: str, name: str) -> dict[str, Any]:
    if kind not in {"filter", "transition", "producer", "consumer", "link"}:
        raise ToolError("kind must be filter, transition, producer, consumer, or link.")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:+-]+", name):
        raise ToolError("Invalid MLT service name.")
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    result = run_capture([str(melt), "-query", f"{kind}={name}"], timeout=30)
    text = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    missing = bool(re.search(r"\bNo metadata for\b", text, re.I))
    return {
        "kind": kind,
        "name": name,
        "available": result.returncode == 0 and bool(text) and not missing,
        "metadata": text[-20000:] or None,
    }


def _extract_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = re.search(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b", value)
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def _safe_service_description(kind: str, name: str) -> dict[str, Any]:
    try:
        return describe_service(kind, name)
    except ToolError as exc:
        return {"available": False, "error": str(exc)}


def compatibility_doctor() -> dict[str, Any]:
    executables = discover_executables()
    repository_error: str | None = None
    repository_ready = False
    if executables.melt is not None:
        try:
            ensure_melt_ready(executables.melt, attempts=3, timeout=5)
            repository_ready = True
        except ToolError as exc:
            repository_error = str(exc)

    shotcut_version, shotcut_error = _safe_version(executables.shotcut, ["--version"])
    mlt_version, mlt_error = _safe_version(executables.melt, ["--version"])
    shotcut_number = _extract_version(shotcut_version)
    mlt_number = _extract_version(mlt_version)

    rnnoise = {
        kind: _safe_service_description(kind, "rnnoise") for kind in ("link", "filter")
    }
    rnnoise_available = any(bool(item.get("available")) for item in rnnoise.values())

    checks: dict[str, dict[str, Any]] = {
        "shotcut": {
            "passed": shotcut_number == (26, 6, 25),
            "expected": "26.6.25",
            "detected": shotcut_version,
            "error": shotcut_error,
        },
        "mlt": {
            "passed": mlt_number is not None and mlt_number[:2] == (7, 40),
            "expected": "7.40.x",
            "detected": mlt_version,
            "error": mlt_error,
        },
        "repository": {
            "passed": repository_ready,
            "error": repository_error,
        },
        "rnnoise": {
            "passed": rnnoise_available,
            "preferred_service": "link",
            "services": rnnoise,
            "note": (
                "RNNoise is checked independently because a successful consumers "
                "preflight does not prove that the RNNoise module loaded."
            ),
        },
    }
    return {
        "compatible": all(check["passed"] for check in checks.values()),
        "validated_stack": {"shotcut": "26.6.25", "mlt": "7.40.x"},
        "checks": checks,
        "path_policy": path_policy(),
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
    enforce_project_resource_policy(project_path)
    if frame < 0:
        raise ToolError("frame must be zero or positive.")
    output = OutputTransaction.prepare(
        output_path, overwrite=overwrite, protected_paths=(project_path,)
    )
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    try:
        result = run_capture(
            [
                str(melt),
                str(project_path),
                f"in={frame}",
                f"out={frame}",
                "-consumer",
                f"avformat:{output.temporary}",
                "f=image2",
                "vcodec=png",
                "real_time=-1",
                "terminate_on_pause=1",
                "-silent",
            ],
            timeout=120,
        )
        if result.returncode or not output.temporary.is_file():
            detail = "\n".join(
                part for part in (result.stdout, result.stderr) if part
            ).strip()
            raise ToolError(
                "Failed to generate preview: "
                + (detail[-2000:] or "output was not created")
            )
        output.commit()
    finally:
        output.cleanup()
    return {
        "created": True,
        "path": str(output_path),
        "frame": frame,
        "size_bytes": output_path.stat().st_size,
    }
