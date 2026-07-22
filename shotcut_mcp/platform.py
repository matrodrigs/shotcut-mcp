"""Public Shotcut/MLT orchestration interface.

Path policy, process supervision, and media inspection are deep modules beneath
this seam. Keeping orchestration here preserves the stable imports used by MCP
handlers and allows tests to replace process functions at the public interface.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from .errors import ToolError
from .media import media_duration, probe_media_raw, summarize_media
from .path_policy import (
    enforce_project_resource_policy,
    expand_path,
    is_network_resource,
    path_policy,
    project_network_resources,
)
from .processes import (
    MLT_ENVIRONMENT_KEYS,
    Executables,
    creation_flags,
    discover_executables,
    require_executable,
    run_capture,
    runtime_identity,
    sys_platform,
    terminate_process,
)
from .storage import OutputTransaction

_SERVICE_CACHE: dict[tuple[object, ...], dict[str, Any]] = {}
_SERVICE_LOCK = threading.Lock()
_MELT_READY_CACHE: set[tuple[object, ...]] = set()
_MELT_READY_LOCK = threading.Lock()

__all__ = [
    "MLT_ENVIRONMENT_KEYS",
    "Executables",
    "compatibility_doctor",
    "creation_flags",
    "describe_service",
    "discover_executables",
    "enforce_project_resource_policy",
    "ensure_melt_ready",
    "expand_path",
    "is_network_resource",
    "list_services",
    "media_duration",
    "open_in_shotcut",
    "path_policy",
    "probe_media_raw",
    "project_network_resources",
    "render_preview",
    "require_executable",
    "run_capture",
    "status",
    "summarize_media",
    "sys_platform",
    "terminate_process",
    "validate_project_file",
    "version_line",
]


def ensure_melt_ready(melt: Path, *, attempts: int = 3, timeout: int = 5) -> None:
    """Warm MLT's module repository and tolerate one-time cold starts.

    A newly installed or extracted Windows build can spend long enough loading
    its DLL-backed modules that an ordinary validation command times out. A
    short terminated attempt warms the operating-system loader; retrying then
    completes normally with the full repository available. Cache readiness by
    executable identity so normal operations pay no repeated startup probe.
    """

    cache_key = runtime_identity(melt)
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
                "SHOTCUT_MCP_MAX_WORKERS",
                "SHOTCUT_MCP_MAX_PENDING",
                "SHOTCUT_MCP_MAX_MESSAGE_BYTES",
                *MLT_ENVIRONMENT_KEYS,
            )
        },
        "path_policy": path_policy(),
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
    cache_key = (*runtime_identity(melt), kind)
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
