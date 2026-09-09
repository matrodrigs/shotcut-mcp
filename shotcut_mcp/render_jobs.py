"""Durable, private storage for background render job state."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict, TypeGuard

from .errors import ToolError
from .protocol import is_array, is_object
from .storage import RenderInputSnapshot, fsync_directory

ProgressSample = TypedDict(
    "ProgressSample",
    {
        "at": float,
        "percent": float | None,
        "frame": int | None,
    },
)
RenderJob = TypedDict(
    "RenderJob",
    {
        "job_id": str,
        "status": str,
        "project_path": str,
        "source_project_path": str,
        "render_project_path": str,
        "editable_project_path": str,
        "output_path": str,
        "temporary_output_path": str,
        "preset": str,
        "melt_path": str,
        "log_path": str,
        "project_revision": str | None,
        "rendered_project_revision": str | None,
        "marker_id": str | None,
        "marker_text": str | None,
        "status_note": str | None,
        "pid": int | None,
        "worker_pid": int | None,
        "renderer_pid": int | None,
        "return_code": int | None,
        "in_frame": int | None,
        "out_frame": int | None,
        "total_frames": int | None,
        "range_duration_frames": int | None,
        "source_duration_frames": int | None,
        "current_frame": int | None,
        "frames_completed": int | None,
        "output_size_bytes": int | None,
        "editable_project_size_bytes": int | None,
        "progress_percent": int | None,
        "started_at": float | None,
        "updated_at": float | None,
        "finished_at": float | None,
        "elapsed_seconds": float | None,
        "average_fps": float | None,
        "overwrite": bool,
        "editable_project_verified": bool,
        "consumer_properties": dict[str, str],
        "output_transaction": dict[str, object],
        "progress_samples": list[ProgressSample],
    },
    total=False,
)


def _is_progress_sample(value: object) -> TypeGuard[ProgressSample]:
    if not is_object(value):
        return False
    at, percent, frame = value.get("at"), value.get("percent"), value.get("frame")
    return (
        isinstance(at, (int, float))
        and not isinstance(at, bool)
        and math.isfinite(at)
        and (
            percent is None
            or (
                isinstance(percent, (int, float))
                and not isinstance(percent, bool)
                and math.isfinite(percent)
            )
        )
        and (frame is None or (isinstance(frame, int) and not isinstance(frame, bool)))
        and all(key in value for key in ("at", "percent", "frame"))
    )


def _is_render_job(value: object) -> TypeGuard[RenderJob]:
    """Validate known persistent fields; preserve unknown fields for compatibility."""
    if not is_object(value) or not isinstance(value.get("job_id"), str):
        return False
    for key in (
        "job_id",
        "status",
        "project_path",
        "source_project_path",
        "render_project_path",
        "editable_project_path",
        "output_path",
        "temporary_output_path",
        "preset",
        "melt_path",
        "log_path",
    ):
        if key in value:
            item = value[key]
            if not (isinstance(item, str)):
                return False
    for key in (
        "project_revision",
        "rendered_project_revision",
        "marker_id",
        "marker_text",
        "status_note",
    ):
        if key in value:
            item = value[key]
            if not (item is None or isinstance(item, str)):
                return False
    for key in (
        "pid",
        "worker_pid",
        "renderer_pid",
        "return_code",
        "in_frame",
        "out_frame",
        "total_frames",
        "range_duration_frames",
        "source_duration_frames",
        "current_frame",
        "frames_completed",
        "output_size_bytes",
        "editable_project_size_bytes",
        "progress_percent",
    ):
        if key in value:
            item = value[key]
            if not (
                item is None or (isinstance(item, int) and not isinstance(item, bool))
            ):
                return False
    for key in (
        "started_at",
        "updated_at",
        "finished_at",
        "elapsed_seconds",
        "average_fps",
    ):
        if key in value:
            item = value[key]
            if not (
                item is None
                or (
                    isinstance(item, (int, float))
                    and not isinstance(item, bool)
                    and math.isfinite(item)
                )
            ):
                return False
    for key in ("overwrite", "editable_project_verified"):
        if key in value:
            item = value[key]
            if not (isinstance(item, bool)):
                return False
    properties = value.get("consumer_properties", {})
    if not is_object(properties) or not all(
        isinstance(item, str) for item in properties.values()
    ):
        return False
    samples = value.get("progress_samples", [])
    return (
        is_object(value.get("output_transaction", {}))
        and is_array(samples)
        and all(_is_progress_sample(sample) for sample in samples)
    )


def _owner_key() -> str:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        return str(getuid())
    home = os.path.normcase(str(Path.home().resolve(strict=False)))
    return hashlib.sha256(home.encode("utf-8")).hexdigest()[:12]


JOB_DIR = Path(tempfile.gettempdir()) / f"shotcut-mcp-{_owner_key()}" / "jobs"
TERMINAL_STATUSES = {
    "cancelled",
    "completed",
    "failed",
    "orphaned",
    "promotion_failed",
}
ALL_STATUSES = TERMINAL_STATUSES | {"queued", "running"}
_WINDOWS_TRANSIENT_FILE_ERRORS = {5, 32, 33}
_WINDOWS_FILE_RETRY_SECONDS = 1.0
_WINDOWS_FILE_RETRY_INTERVAL = 0.01
_IS_WINDOWS = os.name == "nt"


def ensure_job_directory() -> Path:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        JOB_DIR.chmod(0o700)
    return JOB_DIR


def validate_job_id(job_id: object) -> str:
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ToolError(
            "Invalid job_id.",
            code="invalid_render_job_id",
            recommended_action="list_render_jobs_and_retry",
            recommended_tool="list_render_jobs",
        )
    return job_id


def metadata_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.json"


def log_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.log"


def startup_log_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.startup.log"


def cancel_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.cancel"


def gate_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.start"


def release_gate(job_id: str) -> None:
    path = gate_path(job_id)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _is_transient_windows_file_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in _WINDOWS_TRANSIENT_FILE_ERRORS or (
        _IS_WINDOWS and isinstance(exc, PermissionError) and exc.errno == errno.EACCES
    )


def _replace_job_metadata(temporary: Path, path: Path) -> None:
    """Promote job state atomically despite short-lived Windows reader locks."""

    deadline = time.monotonic() + _WINDOWS_FILE_RETRY_SECONDS
    while True:
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:  # noqa: PERF203 - retry is the purpose of this loop
            # Windows readers can briefly deny the delete-sharing permission required
            # by os.replace. Keep retrying the same complete temporary file; never fall
            # back to an in-place write that could expose partial JSON to another process.
            if (
                not _is_transient_windows_file_error(exc)
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(_WINDOWS_FILE_RETRY_INTERVAL)


def _read_job_metadata(path: Path) -> str:
    """Read complete job state despite short-lived Windows writer locks."""

    deadline = time.monotonic() + _WINDOWS_FILE_RETRY_SECONDS
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:  # noqa: PERF203 - retry is the purpose of this loop
            if (
                not _is_transient_windows_file_error(exc)
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(_WINDOWS_FILE_RETRY_INTERVAL)


def write_job(metadata: Mapping[str, object]) -> None:
    path = metadata_path(validate_job_id(metadata.get("job_id", "")))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_job_metadata(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_job(job_id: str) -> RenderJob:
    path = metadata_path(job_id)
    try:
        payload: object = json.loads(_read_job_metadata(path))
    except FileNotFoundError as exc:
        raise ToolError(
            f"Render job not found: {job_id}",
            code="render_job_not_found",
            recommended_action="list_render_jobs_and_retry",
            recommended_tool="list_render_jobs",
            details={"job_id": job_id},
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            f"Invalid render metadata: {exc}",
            code="invalid_render_metadata",
            recoverable=False,
            recommended_action="report_issue",
            details={"job_id": job_id},
        ) from exc
    if not _is_render_job(payload) or payload.get("job_id") != job_id:
        raise ToolError(
            "Invalid render metadata.",
            code="invalid_render_metadata",
            recoverable=False,
            recommended_action="report_issue",
            details={"job_id": job_id},
        )
    return payload


def render_input_snapshot(metadata: Mapping[str, object]) -> RenderInputSnapshot:
    """Validate and reconstruct the project snapshot owned by job metadata."""

    job_id = metadata.get("job_id")
    project_path = metadata.get("source_project_path") or metadata.get("project_path")
    revision = metadata.get("rendered_project_revision") or metadata.get(
        "project_revision"
    )
    render_path = metadata.get("render_project_path")
    editable_path = metadata.get("editable_project_path")
    if (
        not isinstance(job_id, str)
        or not isinstance(project_path, str)
        or not isinstance(revision, str)
    ):
        raise ToolError(
            "Invalid render project snapshot metadata.",
            code="invalid_persistent_render_state",
            recoverable=False,
            recommended_action="report_issue",
            details={"field": "render_project_path", "reason": "missing_identity"},
        )
    try:
        snapshot = RenderInputSnapshot.for_job(Path(project_path), job_id, revision)
    except ValueError as exc:
        raise ToolError(
            "Invalid render project snapshot metadata.",
            code="invalid_persistent_render_state",
            recoverable=False,
            recommended_action="report_issue",
            details={"field": "render_project_path", "reason": str(exc)},
        ) from exc
    if render_path != str(snapshot.path) or editable_path != str(snapshot.path):
        raise ToolError(
            "Render project snapshot path does not match its job identity.",
            code="invalid_persistent_render_state",
            recoverable=False,
            recommended_action="report_issue",
            details={"field": "render_project_path", "reason": "path_mismatch"},
        )
    return snapshot


def request_cancel(job_id: str) -> None:
    path = cancel_path(job_id)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)


def cancel_requested(job_id: str) -> bool:
    return cancel_path(job_id).is_file()


def clear_control_files(job_id: str) -> None:
    cancel_path(job_id).unlink(missing_ok=True)
    gate_path(job_id).unlink(missing_ok=True)


def read_progress(path: Path) -> tuple[int | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 65_536))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None, None
    matches = re.findall(
        r"(?:percentage|percent|progress)\s*[:=]\s*(\d{1,3})", text, re.I
    )
    progress = min(100, int(matches[-1])) if matches else None
    return progress, text[-4000:].strip() or None


def list_jobs(
    *, status: str | None = None, cursor: str | None = None, limit: int = 20
) -> dict[str, object]:
    """Return bounded newest-first immutable render summaries."""

    if status is not None and status not in ALL_STATUSES:
        raise ToolError(f"Invalid render status filter: {status}")
    if not 1 <= limit <= 100:
        raise ToolError("limit must be between 1 and 100.")
    if cursor is not None:
        validate_job_id(cursor)
    jobs: list[RenderJob] = []
    candidates: list[tuple[float, Path]] = []
    for index, path in enumerate(ensure_job_directory().glob("*.json")):
        if index >= 5000:
            break
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    files = [path for _, path in sorted(candidates, reverse=True)[:1000]]
    for path in files:
        try:
            metadata = read_job(path.stem)
        except (OSError, ToolError):
            continue
        if status is None or metadata.get("status") == status:
            jobs.append(metadata)
    jobs.sort(
        key=lambda item: (float(item.get("started_at") or 0), str(item.get("job_id"))),
        reverse=True,
    )
    start = 0
    if cursor is not None:
        positions = [
            index for index, item in enumerate(jobs) if item.get("job_id") == cursor
        ]
        if not positions:
            raise ToolError(
                "Render history cursor was not found for this filter.",
                code="render_history_cursor_not_found",
                recommended_action="restart_render_history_query",
                recommended_tool="list_render_jobs",
                details={"cursor": cursor, "status_filter": status},
            )
        start = positions[0] + 1
    page = jobs[start : start + limit]
    fields = (
        "job_id",
        "status",
        "project_path",
        "rendered_project_revision",
        "editable_project_path",
        "output_path",
        "preset",
        "in_frame",
        "out_frame",
        "marker_id",
        "marker_text",
        "total_frames",
        "range_duration_frames",
        "frames_completed",
        "started_at",
        "updated_at",
        "finished_at",
        "elapsed_seconds",
        "progress_percent",
        "current_frame",
        "return_code",
        "output_size_bytes",
        "average_fps",
        "status_note",
    )
    raw_page: list[Mapping[str, object]] = list(page)
    summaries = [{key: item.get(key) for key in fields} for item in raw_page]
    has_more = start + limit < len(jobs)
    return {
        "jobs": summaries,
        "count": len(summaries),
        "next_cursor": page[-1]["job_id"] if has_more and page else None,
        "status_filter": status,
    }


def prune_jobs(max_age_days: int = 30) -> None:
    directory = ensure_job_directory()
    cutoff = time.time() - max_age_days * 86_400
    for metadata_file in directory.glob("*.json"):
        try:
            if metadata_file.stat().st_mtime >= cutoff:
                continue
            metadata = read_job(metadata_file.stem)
            if metadata.get("status") not in TERMINAL_STATUSES:
                continue
            job_id = metadata_file.stem
            for related in (
                metadata_file,
                log_path(job_id),
                startup_log_path(job_id),
                cancel_path(job_id),
                gate_path(job_id),
            ):
                related.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError, ToolError):
            continue
