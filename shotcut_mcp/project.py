"""Transactional interface for structure-preserving Shotcut project edits.

The public project workflow lives here. XML structure and edit semantics are hidden
behind :class:`ProjectDocument` in ``project_document`` so transaction safety can
evolve independently from the MLT domain model.
"""

from __future__ import annotations

import difflib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConflictError, RequestCancelled, ToolError
from .missing_media import diagnose_missing_resources
from .platform import (
    enforce_project_resource_policy,
    expand_path,
    summarize_media,
    validate_project_file,
)
from .platform import (
    render_contact_sheet as _render_contact_sheet,
)
from .project_document import (
    BACKGROUND_ID,
    MAIN_BIN_IDS,
    SEQUENCE_TAGS,
    TrackRef,
    _boolean,
    _int,
    project_revision,
)
from .project_document import (
    ProjectDocument as MltProjectDocument,
)
from .project_snapshot import build_project_snapshot
from .protocol import cancellation_requested
from .storage import (
    fsync_directory,
    is_project_backup,
    list_project_backups,
    project_lock,
    write_project_backup,
)

MAX_OPERATIONS = 500

__all__ = [
    "BACKGROUND_ID",
    "MAIN_BIN_IDS",
    "MAX_OPERATIONS",
    "SEQUENCE_TAGS",
    "EditCandidate",
    "ProjectDocument",
    "TrackRef",
    "create_project",
    "diagnose_color_workflow",
    "diagnose_missing_media",
    "edit_project",
    "list_backups",
    "plan_project_edit",
    "render_project_contact_sheet",
    "restore_backup",
]


class ProjectDocument(MltProjectDocument):
    """Public project model with the stable MCP inspection projection."""

    def snapshot(self) -> dict[str, Any]:
        return build_project_snapshot(self)


@dataclass
class EditCandidate:
    """Validated in-memory edit awaiting preview or atomic commit."""

    path: Path
    document: ProjectDocument
    original: bytes
    original_revision: str
    expected_revision: str | None
    force: bool
    timeout: int
    operation_results: list[dict[str, Any]]


def _write_validated(
    document: ProjectDocument,
    *,
    expected_revision: str | None,
    force: bool,
    validate: bool,
    timeout: int,
    create_backup: bool,
) -> dict[str, Any]:
    path = document.path
    data = document.to_bytes()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp{path.suffix}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with project_lock(path):
        current = path.read_bytes() if path.is_file() else None
        current_mode = path.stat().st_mode if current is not None else None
        current_revision = project_revision(current) if current is not None else None
        if current is not None and not force:
            if not expected_revision:
                raise ConflictError(
                    "expected_revision is required to edit an existing project.",
                    current_revision=current_revision,
                )
            if expected_revision != current_revision:
                raise ConflictError(
                    f"The project changed. Expected {expected_revision}, current "
                    f"{current_revision}.",
                    expected_revision=expected_revision,
                    current_revision=current_revision,
                )
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if current_mode is not None:
            os.chmod(temporary, current_mode)
        try:
            validation = (
                validate_project_file(temporary, timeout=timeout)
                if validate
                else {"valid": True}
            )
            if not validation.get("valid"):
                raise ToolError(
                    "MLT rejected the edit before the project was replaced: "
                    + str(validation.get("diagnostic") or validation.get("return_code"))
                )
            latest = path.read_bytes() if path.is_file() else None
            latest_revision = project_revision(latest) if latest is not None else None
            if latest_revision != current_revision:
                raise ConflictError(
                    "The project changed while the candidate edit was being validated. "
                    f"Expected {current_revision}, current {latest_revision}.",
                    expected_revision=current_revision,
                    current_revision=latest_revision,
                )
            backup_path = (
                write_project_backup(path, current)
                if current is not None and create_backup
                else None
            )
            os.replace(temporary, path)
            fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    revision = project_revision(data)
    return {
        "path": str(path),
        "revision": revision,
        "previous_revision": current_revision,
        "backup_path": str(backup_path) if backup_path else None,
        "validation": validation,
    }


def create_project(arguments: dict[str, Any]) -> dict[str, Any]:
    path = expand_path(arguments.get("project_path", ""))
    if path.suffix.lower() not in {".mlt", ".xml"}:
        raise ToolError("The project must use the .mlt or .xml extension.")
    overwrite = _boolean(arguments.get("overwrite", False), "overwrite")
    if path.exists() and not overwrite:
        raise ToolError(f"The project already exists: {path}")
    width = _int(arguments.get("width", 1920), "width", 16)
    height = _int(arguments.get("height", 1080), "height", 16)
    fps_num = _int(arguments.get("fps_num", 30), "fps_num", 1)
    fps_den = _int(arguments.get("fps_den", 1), "fps_den", 1)
    document = ProjectDocument.new(
        path,
        width=width,
        height=height,
        fps_num=fps_num,
        fps_den=fps_den,
        title=arguments.get("notes", "")
        if isinstance(arguments.get("notes", ""), str)
        else "",
    )
    tracks = arguments.get("tracks", [])
    if not isinstance(tracks, list):
        raise ToolError("tracks must be a list.")
    results = [document.add_track({"op": "add_track", **track}) for track in tracks]
    clips = arguments.get("clips", [])
    if not isinstance(clips, list):
        raise ToolError("clips must be a list.")
    for clip in clips:
        operation = _authorize_operation_paths(
            {"op": "add_clip", "track": "V1", **clip}
        )
        results.append(document.add_clip(operation))
    document.update_main_duration()
    saved = _write_validated(
        document,
        expected_revision=project_revision(path.read_bytes())
        if path.exists()
        else None,
        force=overwrite,
        validate=True,
        timeout=_int(arguments.get("timeout_seconds", 60), "timeout_seconds", 1),
        create_backup=path.exists(),
    )
    loaded = ProjectDocument.load(path)
    return {
        "created": True,
        **saved,
        "operation_results": results,
        "project": loaded.snapshot(),
    }


def _authorize_operation_paths(operation: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize every data path before it reaches the XML domain model."""

    result = dict(operation)
    name = result.get("op")
    field = "path" if name == "add_clip" else "to" if name == "relink_media" else None
    if field is not None and field in result:
        value = result[field]
        if not isinstance(value, str):
            raise ToolError(f"{field} must be a string.")
        result[field] = str(expand_path(value))
    return result


def _build_edit_candidate(arguments: dict[str, Any]) -> EditCandidate:
    path = expand_path(arguments.get("project_path", ""))
    operations = arguments.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ToolError("operations must be a non-empty list.")
    if len(operations) > MAX_OPERATIONS:
        raise ToolError(f"A transaction accepts at most {MAX_OPERATIONS} operations.")
    force = _boolean(arguments.get("force", False), "force")
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and not isinstance(expected_revision, str):
        raise ToolError("expected_revision must be a SHA-256 string.")
    document = ProjectDocument.load(path)
    original = document.source
    original_revision = document.revision
    if not force:
        if not expected_revision:
            raise ConflictError(
                "expected_revision is required to edit an existing project.",
                current_revision=original_revision,
            )
        if expected_revision != original_revision:
            raise ConflictError(
                f"The project changed. Expected {expected_revision}, current "
                f"{original_revision}.",
                expected_revision=expected_revision,
                current_revision=original_revision,
            )
    document.ensure_shotcut_structure()
    results: list[dict[str, Any]] = []
    for index, raw_operation in enumerate(operations):
        if cancellation_requested():
            raise RequestCancelled("Project edit cancelled by the MCP client.")
        try:
            if not isinstance(raw_operation, dict):
                raise ToolError("Each operation must be an object.")
            operation = _authorize_operation_paths(raw_operation)
            results.append(document.apply_operation(operation))
        except ToolError as exc:
            raise ToolError(f"Operation {index} failed: {exc}") from exc
    document.update_main_duration()
    return EditCandidate(
        path=path,
        document=document,
        original=original,
        original_revision=original_revision,
        expected_revision=expected_revision,
        force=force,
        timeout=_int(arguments.get("timeout_seconds", 60), "timeout_seconds", 1),
        operation_results=results,
    )


def plan_project_edit(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("force") not in (None, False):
        raise ToolError("force is not supported by plan_project_edit.")
    candidate = _build_edit_candidate(arguments)
    data = candidate.document.to_bytes()
    prospective_revision = project_revision(data)
    temporary = candidate.path.with_name(
        f".{candidate.path.name}.{uuid.uuid4().hex}.plan{candidate.path.suffix}"
    )
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        validation = validate_project_file(temporary, timeout=candidate.timeout)
    finally:
        temporary.unlink(missing_ok=True)
    latest = candidate.path.read_bytes() if candidate.path.is_file() else None
    latest_revision = project_revision(latest) if latest is not None else None
    if latest_revision != candidate.original_revision:
        raise ConflictError(
            "The project changed while the planned edit was being validated.",
            expected_revision=candidate.original_revision,
            current_revision=latest_revision,
        )

    maximum_lines = _int(arguments.get("max_diff_lines", 2000), "max_diff_lines", 0)
    maximum_lines = min(maximum_lines, 5000)
    diff_lines = list(
        difflib.unified_diff(
            candidate.original.decode("utf-8", errors="replace").splitlines(),
            data.decode("utf-8", errors="replace").splitlines(),
            fromfile=str(candidate.path),
            tofile=f"{candidate.path} (planned)",
            lineterm="",
        )
    )
    diff_truncated = len(diff_lines) > maximum_lines
    shown_lines = diff_lines[:maximum_lines]
    candidate.document.source = data
    candidate.document.revision = prospective_revision
    return {
        "planned": True,
        "changed": data != candidate.original,
        "project_path": str(candidate.path),
        "base_revision": candidate.original_revision,
        "prospective_revision": prospective_revision,
        "operation_results": candidate.operation_results,
        "validation": validation,
        "project": candidate.document.snapshot(),
        "unified_diff": "\n".join(shown_lines),
        "diff_lines": len(diff_lines),
        "diff_truncated": diff_truncated,
    }


def edit_project(arguments: dict[str, Any]) -> dict[str, Any]:
    candidate = _build_edit_candidate(arguments)
    saved = _write_validated(
        candidate.document,
        expected_revision=candidate.expected_revision,
        force=candidate.force,
        validate=True,
        timeout=candidate.timeout,
        create_backup=True,
    )
    updated = ProjectDocument.load(candidate.path)
    return {
        "edited": True,
        **saved,
        "operation_results": candidate.operation_results,
        "project": updated.snapshot(),
    }


def list_backups(project_path: Path) -> dict[str, Any]:
    backups = [
        {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "modified_at": path.stat().st_mtime,
            "revision": project_revision(path.read_bytes()),
        }
        for path in list_project_backups(project_path)
    ]
    return {
        "project_path": str(project_path),
        "backup_count": len(backups),
        "backups": backups,
    }


def diagnose_color_workflow(arguments: dict[str, Any]) -> dict[str, Any]:
    """Report project/source color compatibility without changing the document."""

    project_path = expand_path(arguments.get("project_path", ""))
    enforce_project_resource_policy(project_path)
    document = ProjectDocument.load(project_path)
    snapshot = document.snapshot()
    media: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resource in snapshot["resources"]:
        resolved = resource.get("resolved_path")
        if not resolved or resource.get("exists") is not True or resolved in seen:
            continue
        seen.add(resolved)
        if len(media) >= 128:
            break
        try:
            media.append(summarize_media(Path(resolved)))
        except ToolError as exc:
            media.append({"path": resolved, "error": str(exc)})
    ranges = {
        stream.get("dynamic_range")
        for item in media
        for stream in item.get("streams", [])
        if stream.get("type") == "video" and stream.get("dynamic_range") != "unknown"
    }
    color = snapshot["color_workflow"]
    mode = color["processing_mode"]
    issues: list[dict[str, str]] = []
    if {"hlg", "pq"}.issubset(ranges):
        issues.append(
            {
                "severity": "error",
                "code": "mixed_hdr_transfers",
                "message": "Shotcut 26.6 cannot combine HLG and PQ in one project.",
            }
        )
    if ranges.intersection({"hlg", "pq"}) and mode == "Native8Cpu":
        issues.append(
            {
                "severity": "error",
                "code": "hdr_requires_10bit_processing",
                "message": "HDR sources require a supported 10-bit processing mode.",
            }
        )
    requested_codec = arguments.get("output_codec")
    hdr_metadata = arguments.get("hdr10_metadata", False)
    if hdr_metadata and not isinstance(hdr_metadata, bool):
        raise ToolError("hdr10_metadata must be a boolean.")
    if requested_codec is not None and not isinstance(requested_codec, str):
        raise ToolError("output_codec must be a string.")
    hardware_tokens = ("_nvenc", "_qsv", "_amf", "_mf", "_vaapi", "videotoolbox")
    if (
        hdr_metadata
        and requested_codec
        and any(token in requested_codec for token in hardware_tokens)
    ):
        issues.append(
            {
                "severity": "error",
                "code": "hdr10_metadata_hardware_encoder",
                "message": "Shotcut 26.6 supports HDR10 metadata only with libx265 or libsvtav1.",
            }
        )
    if color["dynamic_range"] in {"hlg", "pq"}:
        unverified = sorted(
            {
                item.get("service")
                for item in [*snapshot["filters"]]
                if item.get("service")
            }
        )
        if unverified:
            issues.append(
                {
                    "severity": "warning",
                    "code": "filters_require_10bit_verification",
                    "message": "Project filters require #10bit/#gpu verification: "
                    + ", ".join(unverified[:20]),
                }
            )
    return {
        "project_path": str(project_path),
        "compatible": not any(item["severity"] == "error" for item in issues),
        "project_color_workflow": color,
        "source_dynamic_ranges": sorted(ranges),
        "media": media,
        "issues": issues,
        "media_truncated": len(seen) > len(media),
    }


def render_project_contact_sheet(arguments: dict[str, Any]) -> dict[str, Any]:
    """Choose project frames and delegate atomic contact-sheet rendering."""

    project_path = expand_path(arguments.get("project_path", ""))
    raw_frames = arguments.get("frames")
    if raw_frames is None:
        sample_count = _int(arguments.get("sample_count", 12), "sample_count", 1)
        if sample_count > 64:
            raise ToolError("sample_count must be an integer between 1 and 64.")
        duration = ProjectDocument.load(project_path).snapshot()["duration_frames"]
        if duration <= 0:
            raise ToolError("The project has no timeline frames to sample.")
        frames = (
            [0]
            if sample_count == 1
            else [
                round(index * (duration - 1) / (sample_count - 1))
                for index in range(sample_count)
            ]
        )
    else:
        if not isinstance(raw_frames, list) or not 1 <= len(raw_frames) <= 64:
            raise ToolError("frames must contain between 1 and 64 entries.")
        frames = []
        for index, frame in enumerate(raw_frames):
            if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
                raise ToolError(f"frames[{index}] must be a non-negative integer.")
            frames.append(frame)
    columns = arguments.get("columns", 4)
    cell_width = arguments.get("cell_width", 320)
    overwrite = arguments.get("overwrite", False)
    if isinstance(columns, bool) or not isinstance(columns, int):
        raise ToolError("columns must be an integer.")
    if isinstance(cell_width, bool) or not isinstance(cell_width, int):
        raise ToolError("cell_width must be an integer.")
    if not isinstance(overwrite, bool):
        raise ToolError("overwrite must be a boolean.")
    raw_output = arguments.get("output_path")
    if raw_output is not None and not isinstance(raw_output, str):
        raise ToolError("output_path must be a string when provided.")
    return _render_contact_sheet(
        project_path,
        expand_path(raw_output) if isinstance(raw_output, str) else None,
        frames,
        columns=columns,
        cell_width=cell_width,
        overwrite=overwrite,
    )


def diagnose_missing_media(arguments: dict[str, Any]) -> dict[str, Any]:
    """Search authorized roots for bounded, scored missing-media candidates."""

    project_path = expand_path(arguments.get("project_path", ""))
    snapshot = ProjectDocument.load(project_path).snapshot()
    diagnosis = diagnose_missing_resources(snapshot["resources"], arguments)
    return {
        "project_path": str(project_path),
        **diagnosis,
        "commit_workflow": "Choose a candidate and call plan_project_edit/edit_project with relink_media and expected_revision.",
    }


def restore_backup(arguments: dict[str, Any]) -> dict[str, Any]:
    project_path = expand_path(arguments.get("project_path", ""))
    backup_path = expand_path(arguments.get("backup_path", ""))
    force = _boolean(arguments.get("force", False), "force")
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and not isinstance(expected_revision, str):
        raise ToolError("expected_revision must be a SHA-256 string.")
    if not is_project_backup(project_path, backup_path):
        raise ToolError("backup_path is not one of this project's backups.")
    document = ProjectDocument.load(backup_path)
    document.path = project_path
    return {
        "restored": True,
        **_write_validated(
            document,
            expected_revision=expected_revision,
            force=force,
            validate=True,
            timeout=60,
            create_backup=True,
        ),
    }
