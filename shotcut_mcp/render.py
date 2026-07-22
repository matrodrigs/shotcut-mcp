"""Background render job management."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import RequestCancelled, ToolError
from .platform import (
    creation_flags,
    discover_executables,
    enforce_project_resource_policy,
    ensure_melt_ready,
    require_executable,
)
from .protocol import cancellation_requested
from .render_jobs import (
    TERMINAL_STATUSES,
    list_jobs,
    log_path,
    prune_jobs,
    read_job,
    read_progress,
    release_gate,
    request_cancel,
    write_job,
)
from .storage import OutputTransaction, process_is_alive

RUNNING_JOBS: dict[str, subprocess.Popen[Any]] = {}
_RUNNING_JOBS_LOCK = threading.Lock()

RENDER_PRESETS: dict[str, dict[str, str]] = {
    "h264-high": {
        "f": "mp4",
        "vcodec": "libx264",
        "crf": "18",
        "preset": "medium",
        "acodec": "aac",
        "ab": "192k",
        "movflags": "+faststart",
    },
    "h264-web": {
        "f": "mp4",
        "vcodec": "libx264",
        "crf": "23",
        "preset": "medium",
        "acodec": "aac",
        "ab": "160k",
        "movflags": "+faststart",
    },
    "hevc": {
        "f": "mp4",
        "vcodec": "libx265",
        "crf": "22",
        "preset": "medium",
        "acodec": "aac",
        "ab": "192k",
        "movflags": "+faststart",
    },
    "hdr-hlg-hevc": {
        "f": "mp4",
        "vcodec": "libx265",
        "crf": "20",
        "preset": "medium",
        "pix_fmt": "yuv420p10le",
        "color_primaries": "bt2020",
        "color_trc": "arib-std-b67",
        "colorspace": "bt2020nc",
        "acodec": "aac",
        "ab": "192k",
        "movflags": "+faststart",
    },
    "hdr-pq-hevc": {
        "f": "mp4",
        "vcodec": "libx265",
        "crf": "20",
        "preset": "medium",
        "pix_fmt": "yuv420p10le",
        "color_primaries": "bt2020",
        "color_trc": "smpte2084",
        "colorspace": "bt2020nc",
        "acodec": "aac",
        "ab": "192k",
        "movflags": "+faststart",
    },
    "av1": {
        "f": "mp4",
        "vcodec": "libsvtav1",
        "crf": "28",
        "preset": "8",
        "acodec": "aac",
        "ab": "192k",
        "movflags": "+faststart",
    },
    "prores": {
        "f": "mov",
        "vcodec": "prores_ks",
        "profile": "3",
        "acodec": "pcm_s24le",
    },
    "dnxhd": {"f": "mov", "vcodec": "dnxhd", "vb": "145M", "acodec": "pcm_s24le"},
    "audio-flac": {"f": "flac", "vn": "1", "acodec": "flac"},
    "audio-mp3": {"f": "mp3", "vn": "1", "acodec": "libmp3lame", "ab": "192k"},
}

SAFE_CONSUMER_PROPERTIES = {
    "ab",
    "acodec",
    "an",
    "ar",
    "aspect",
    "bf",
    "channels",
    "color_primaries",
    "color_range",
    "color_trc",
    "colorspace",
    "crf",
    "f",
    "g",
    "height",
    "movflags",
    "pix_fmt",
    "preset",
    "progressive",
    "r",
    "rescale",
    "strict",
    "threads",
    "top_field_first",
    "vb",
    "vcodec",
    "video_track_timescale",
    "vn",
    "width",
}
SAFE_SINGLE_FILE_FORMATS = {
    "avi",
    "flac",
    "matroska",
    "mov",
    "mp3",
    "mp4",
    "mpegts",
    "mxf",
    "ogg",
    "wav",
    "webm",
}


def _consumer_properties(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 50:
        raise ToolError(
            "consumer_properties must be an object with at most 50 options."
        )
    result: dict[str, str] = {}
    unsafe_allowed = os.environ.get(
        "SHOTCUT_MCP_ALLOW_UNSAFE_CONSUMER_PROPERTIES", ""
    ).lower() in {"1", "true", "yes"}
    for key, raw in value.items():
        if not isinstance(key, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.:-]*", key
        ):
            raise ToolError(f"Invalid MLT property name: {key!r}")
        if key in {"target", "resource"}:
            raise ToolError(f"The {key} property is controlled by the server.")
        if not unsafe_allowed and key not in SAFE_CONSUMER_PROPERTIES:
            raise ToolError(
                f"consumer_properties.{key} is not in the safe allowlist. "
                "An administrator can opt into arbitrary MLT properties with "
                "SHOTCUT_MCP_ALLOW_UNSAFE_CONSUMER_PROPERTIES=1."
            )
        if isinstance(raw, bool):
            text = "1" if raw else "0"
        elif isinstance(raw, (str, int, float)):
            text = str(raw)
        else:
            raise ToolError(f"Invalid value for consumer_properties.{key}.")
        if len(text) > 500:
            raise ToolError(f"consumer_properties.{key} exceeds 500 characters.")
        if (
            not unsafe_allowed
            and key == "f"
            and text.lower() not in SAFE_SINGLE_FILE_FORMATS
        ):
            raise ToolError(
                f"consumer_properties.f={text!r} may create sidecar files and is "
                "not in the safe allowlist."
            )
        result[key] = text
    return result


def start_render(arguments: dict[str, Any]) -> dict[str, Any]:
    from .platform import expand_path

    project_path = expand_path(arguments.get("project_path", ""))
    output_path = expand_path(arguments.get("output_path", ""))
    if not project_path.is_file():
        raise ToolError(f"Project not found: {project_path}")
    enforce_project_resource_policy(project_path)
    overwrite = arguments.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ToolError("overwrite must be a boolean.")

    preset = arguments.get("preset", "h264-high")
    if preset not in RENDER_PRESETS:
        raise ToolError(f"Invalid preset. Options: {', '.join(RENDER_PRESETS)}")
    properties = dict(RENDER_PRESETS[preset])
    properties.update(_consumer_properties(arguments.get("consumer_properties")))
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    output = OutputTransaction.prepare(
        output_path, overwrite=overwrite, protected_paths=(project_path,)
    )
    prune_jobs()
    job_id = uuid.uuid4().hex
    metadata = {
        "job_id": job_id,
        "pid": None,
        "worker_pid": None,
        "renderer_pid": None,
        "status": "queued",
        "return_code": None,
        "project_path": str(project_path),
        "output_path": str(output_path),
        "temporary_output_path": str(output.temporary),
        "output_transaction": output.serialize(),
        "overwrite": overwrite,
        "preset": preset,
        "consumer_properties": properties,
        "melt_path": str(melt),
        "log_path": str(log_path(job_id)),
        "started_at": time.time(),
        "updated_at": time.time(),
        "finished_at": None,
        "progress_percent": 0,
        "current_frame": None,
        "progress_samples": [],
    }
    write_job(metadata)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "shotcut_mcp.render_worker", job_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags(),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        output.cleanup()
        metadata.update(
            status="failed",
            status_note=f"Could not start the render supervisor: {exc}",
            finished_at=time.time(),
        )
        write_job(metadata)
        raise ToolError(f"Could not start the render: {exc}") from exc
    with _RUNNING_JOBS_LOCK:
        RUNNING_JOBS[job_id] = process
    threading.Thread(
        target=_reap_supervisor, args=(job_id, process), daemon=True
    ).start()
    metadata.update(pid=process.pid, worker_pid=process.pid, status="running")
    write_job(metadata)
    release_gate(job_id)
    return metadata


def _reap_supervisor(job_id: str, process: subprocess.Popen[Any]) -> None:
    """Release local process handles even when no client polls render status."""

    process.wait()
    # Preserve the short compatibility window used by callers that inspect an
    # immediately returned local process while still guaranteeing eventual cleanup.
    time.sleep(1)
    with _RUNNING_JOBS_LOCK:
        if RUNNING_JOBS.get(job_id) is process:
            RUNNING_JOBS.pop(job_id, None)


def _eta(metadata: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    samples = [
        item
        for item in metadata.get("progress_samples") or []
        if isinstance(item, dict)
        and isinstance(item.get("at"), (int, float))
        and isinstance(item.get("percent"), (int, float))
    ]
    advancing = [
        item
        for index, item in enumerate(samples)
        if index == 0 or item["percent"] > samples[index - 1]["percent"]
    ]
    if len(advancing) < 2:
        return None, None, None
    first, last = advancing[0], advancing[-1]
    elapsed = float(last["at"]) - float(first["at"])
    progress = float(last["percent"])
    gained = progress - float(first["percent"])
    if elapsed < 1 or gained <= 0 or progress <= 0 or progress >= 100:
        return None, None, None
    rate = gained / elapsed
    eta = (100 - progress) / rate
    confidence = "high" if progress >= 30 else "medium" if progress >= 10 else "low"
    return max(0.0, eta), confidence, "smoothed_progress_percent"


def render_status(job_id: str) -> dict[str, Any]:
    metadata = read_job(job_id)
    with _RUNNING_JOBS_LOCK:
        process = RUNNING_JOBS.get(job_id)
    if process is not None and process.poll() is not None:
        with _RUNNING_JOBS_LOCK:
            RUNNING_JOBS.pop(job_id, None)
        metadata = read_job(job_id)
    if metadata.get("status") not in TERMINAL_STATUSES:
        worker_pid = metadata.get("worker_pid")
        if isinstance(worker_pid, int) and not process_is_alive(worker_pid):
            metadata = read_job(job_id)
            if metadata.get("status") not in TERMINAL_STATUSES:
                renderer_pid = metadata.get("renderer_pid")
                renderer_alive = isinstance(renderer_pid, int) and process_is_alive(
                    renderer_pid
                )
                if renderer_alive:
                    metadata.update(
                        status="orphaned",
                        status_note=(
                            "The render supervisor exited while Melt was still running; "
                            "the temporary output was retained."
                        ),
                        finished_at=time.time(),
                    )
                else:
                    metadata.update(
                        status="failed",
                        status_note=(
                            "The render supervisor exited before finalizing the job."
                        ),
                        finished_at=time.time(),
                    )
                    OutputTransaction.deserialize(
                        metadata.get("output_transaction")
                    ).cleanup()
                write_job(metadata)
        elif (
            worker_pid is None
            and time.time() - float(metadata.get("started_at", 0)) > 10
        ):
            metadata.update(
                status="failed",
                status_note="The render supervisor never started.",
                finished_at=time.time(),
            )
            OutputTransaction.deserialize(metadata.get("output_transaction")).cleanup()
            write_job(metadata)
    output_path = Path(metadata["output_path"])
    progress, log_tail = read_progress(Path(metadata["log_path"]))
    metadata["progress_percent"] = (
        100
        if metadata.get("status") == "completed"
        else progress
        if progress is not None
        else metadata.get("progress_percent")
    )
    metadata["output_exists"] = output_path.is_file()
    metadata["output_size_bytes"] = (
        output_path.stat().st_size if output_path.is_file() else None
    )
    metadata["log_tail"] = log_tail
    now = float(metadata.get("finished_at") or time.time())
    metadata["elapsed_seconds"] = max(
        0.0, now - float(metadata.get("started_at") or now)
    )
    eta_seconds, eta_confidence, eta_basis = (
        (None, None, None)
        if metadata.get("status") in TERMINAL_STATUSES
        else _eta(metadata)
    )
    metadata["eta_seconds"] = eta_seconds
    metadata["eta_confidence"] = eta_confidence
    metadata["eta_basis"] = eta_basis
    return metadata


def list_render_jobs(arguments: dict[str, Any]) -> dict[str, Any]:
    status = arguments.get("status")
    cursor = arguments.get("cursor")
    limit = arguments.get("limit", 20)
    if status is not None and not isinstance(status, str):
        raise ToolError("status must be a string.")
    if cursor is not None and not isinstance(cursor, str):
        raise ToolError("cursor must be a string.")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ToolError("limit must be an integer.")
    return list_jobs(status=status, cursor=cursor, limit=limit)


def cancel_render(job_id: str) -> dict[str, Any]:
    metadata = read_job(job_id)
    if metadata.get("status") in TERMINAL_STATUSES:
        return render_status(job_id)
    request_cancel(job_id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if cancellation_requested():
            raise RequestCancelled("Render cancellation request was itself cancelled.")
        metadata = read_job(job_id)
        if metadata.get("status") in TERMINAL_STATUSES:
            return render_status(job_id)
        time.sleep(0.05)
    result = render_status(job_id)
    result["cancellation_requested"] = True
    return result
