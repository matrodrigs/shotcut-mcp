"""Background render job management."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import ToolError
from .platform import (
    creation_flags,
    discover_executables,
    ensure_melt_ready,
    require_executable,
)


JOB_DIR = Path(tempfile.gettempdir()) / "shotcut-mcp" / "jobs"
JOB_DIR.mkdir(parents=True, exist_ok=True)
RUNNING_JOBS: dict[str, subprocess.Popen[Any]] = {}

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


def _metadata_path(job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ToolError("Invalid job_id.")
    return JOB_DIR / f"{job_id}.json"


def _write_job(metadata: dict[str, Any]) -> None:
    path = _metadata_path(metadata["job_id"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_job(job_id: str) -> dict[str, Any]:
    path = _metadata_path(job_id)
    if not path.is_file():
        raise ToolError(f"Render job not found: {job_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"Invalid render metadata: {exc}") from exc
    if not isinstance(payload, dict):
        raise ToolError("Invalid render metadata.")
    return payload


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
    if project_path == output_path:
        raise ToolError("Project and output cannot be the same file.")
    overwrite = arguments.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ToolError("overwrite must be a boolean.")
    if output_path.exists():
        if not output_path.is_file():
            raise ToolError(f"The existing output is not a file: {output_path}")
        if not overwrite:
            raise ToolError(f"The output already exists: {output_path}")

    preset = arguments.get("preset", "h264-high")
    if preset not in RENDER_PRESETS:
        raise ToolError(f"Invalid preset. Options: {', '.join(RENDER_PRESETS)}")
    properties = dict(RENDER_PRESETS[preset])
    properties.update(_consumer_properties(arguments.get("consumer_properties")))
    melt = require_executable(discover_executables().melt, "melt", "SHOTCUT_MELT_PATH")
    ensure_melt_ready(melt)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    temporary_output = output_path.with_name(
        f".{output_path.stem}.{job_id}.shotcut-mcp{output_path.suffix}"
    )
    log_path = JOB_DIR / f"{job_id}.log"
    command = [
        str(melt),
        str(project_path),
        "-progress",
        "-consumer",
        f"avformat:{temporary_output}",
        "real_time=-1",
        "terminate_on_pause=1",
        *[f"{key}={value}" for key, value in properties.items()],
    ]
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creation_flags(),
                start_new_session=os.name != "nt",
            )
    except OSError as exc:
        raise ToolError(f"Could not start the render: {exc}") from exc
    RUNNING_JOBS[job_id] = process
    metadata = {
        "job_id": job_id,
        "pid": process.pid,
        "status": "running",
        "return_code": None,
        "project_path": str(project_path),
        "output_path": str(output_path),
        "temporary_output_path": str(temporary_output),
        "overwrite": overwrite,
        "preset": preset,
        "consumer_properties": properties,
        "log_path": str(log_path),
        "started_at": time.time(),
        "finished_at": None,
    }
    _write_job(metadata)
    return metadata


def _finish_render(metadata: dict[str, Any], return_code: int) -> None:
    temporary = Path(metadata["temporary_output_path"])
    output = Path(metadata["output_path"])
    metadata["return_code"] = return_code
    metadata["finished_at"] = time.time()
    if return_code != 0 or not temporary.is_file():
        metadata["status"] = "failed"
        temporary.unlink(missing_ok=True)
        return
    if output.exists() and not metadata.get("overwrite", False):
        metadata["status"] = "failed"
        metadata["status_note"] = (
            "The output appeared during rendering; the new file was preserved to avoid overwriting it."
        )
        return
    try:
        os.replace(temporary, output)
    except OSError as exc:
        metadata["status"] = "failed"
        metadata["status_note"] = (
            f"The render completed, but atomic promotion failed: {exc}"
        )
        return
    metadata["status"] = "completed"


def _progress(log_path: Path) -> tuple[int | None, str | None]:
    if not log_path.is_file():
        return None, None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    matches = re.findall(
        r"(?:percentage|percent|progress)\s*[:=]\s*(\d{1,3})", text, re.I
    )
    progress = min(100, int(matches[-1])) if matches else None
    return progress, text[-4000:].strip() or None


def render_status(job_id: str) -> dict[str, Any]:
    metadata = _read_job(job_id)
    process = RUNNING_JOBS.get(job_id)
    if process is not None:
        return_code = process.poll()
        if return_code is not None and metadata.get("status") == "running":
            _finish_render(metadata, return_code)
            _write_job(metadata)
            RUNNING_JOBS.pop(job_id, None)
    elif metadata.get("status") == "running":
        metadata["status"] = "detached"
        metadata["status_note"] = (
            "The MCP server restarted; the process may continue, but it can no longer be cancelled safely."
        )
    output_path = Path(metadata["output_path"])
    progress, log_tail = _progress(Path(metadata["log_path"]))
    metadata["progress_percent"] = (
        100 if metadata.get("status") == "completed" else progress
    )
    metadata["output_exists"] = output_path.is_file()
    metadata["output_size_bytes"] = (
        output_path.stat().st_size if output_path.is_file() else None
    )
    metadata["log_tail"] = log_tail
    return metadata


def cancel_render(job_id: str) -> dict[str, Any]:
    metadata = _read_job(job_id)
    process = RUNNING_JOBS.get(job_id)
    if process is None:
        raise ToolError("This render is not active in the current MCP session.")
    if process.poll() is not None:
        return render_status(job_id)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    metadata.update(
        status="cancelled", return_code=process.returncode, finished_at=time.time()
    )
    temporary = Path(metadata.get("temporary_output_path", ""))
    if temporary.name:
        temporary.unlink(missing_ok=True)
    _write_job(metadata)
    RUNNING_JOBS.pop(job_id, None)
    return metadata
