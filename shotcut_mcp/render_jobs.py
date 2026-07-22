"""Durable, private storage for background render job state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import ToolError
from .storage import fsync_directory


def _owner_key() -> str:
    if hasattr(os, "getuid"):
        return str(os.getuid())
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


def ensure_job_directory() -> Path:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        JOB_DIR.chmod(0o700)
    return JOB_DIR


def validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ToolError("Invalid job_id.")
    return job_id


def metadata_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.json"


def log_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.log"


def cancel_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.cancel"


def gate_path(job_id: str) -> Path:
    return ensure_job_directory() / f"{validate_job_id(job_id)}.start"


def release_gate(job_id: str) -> None:
    path = gate_path(job_id)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)


def write_job(metadata: dict[str, Any]) -> None:
    path = metadata_path(metadata.get("job_id", ""))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_job(job_id: str) -> dict[str, Any]:
    path = metadata_path(job_id)
    if not path.is_file():
        raise ToolError(f"Render job not found: {job_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"Invalid render metadata: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("job_id") != job_id:
        raise ToolError("Invalid render metadata.")
    return payload


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
) -> dict[str, Any]:
    """Return bounded newest-first immutable render summaries."""

    if status is not None and status not in ALL_STATUSES:
        raise ToolError(f"Invalid render status filter: {status}")
    if not 1 <= limit <= 100:
        raise ToolError("limit must be between 1 and 100.")
    if cursor is not None:
        validate_job_id(cursor)
    jobs: list[dict[str, Any]] = []
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
            raise ToolError("Render history cursor was not found for this filter.")
        start = positions[0] + 1
    page = jobs[start : start + limit]
    fields = (
        "job_id",
        "status",
        "project_path",
        "output_path",
        "preset",
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
    summaries = [{key: item.get(key) for key in fields} for item in page]
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
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            if metadata.get("status") not in TERMINAL_STATUSES:
                continue
            job_id = metadata_file.stem
            for related in (
                metadata_file,
                log_path(job_id),
                cancel_path(job_id),
                gate_path(job_id),
            ):
                related.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError, ToolError):
            continue
