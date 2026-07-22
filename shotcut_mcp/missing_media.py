"""Bounded missing-media discovery behind one project-facing interface.

This module owns filesystem traversal, candidate scoring, media probing, and optional
visualization.  Keeping those details here lets ``project`` expose a small workflow
without teaching the MCP routing layer how relink diagnosis works.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import RequestCancelled, ToolError
from .platform import (
    expand_path,
    render_media_contact_sheet,
    shotcut_file_hash,
    summarize_media,
)
from .protocol import cancellation_requested


@dataclass(frozen=True)
class _SearchLimits:
    max_depth: int
    max_files: int
    max_candidates: int
    timeout_seconds: int
    max_hash_bytes: int
    max_probe_candidates: int

    @classmethod
    def from_arguments(cls, arguments: dict[str, Any]) -> _SearchLimits:
        limits = cls(
            max_depth=_integer(arguments.get("max_depth", 6), "max_depth", 0),
            max_files=_integer(arguments.get("max_files", 5000), "max_files", 1),
            max_candidates=_integer(
                arguments.get("max_candidates_per_resource", 10),
                "max_candidates_per_resource",
                1,
            ),
            timeout_seconds=_integer(
                arguments.get("timeout_seconds", 30), "timeout_seconds", 1
            ),
            max_hash_bytes=_integer(
                arguments.get("max_hash_bytes", 256 * 1024 * 1024),
                "max_hash_bytes",
                0,
            ),
            max_probe_candidates=_integer(
                arguments.get("max_probe_candidates", 128),
                "max_probe_candidates",
                0,
            ),
        )
        if (
            limits.max_depth > 16
            or limits.max_files > 20_000
            or limits.max_candidates > 50
            or limits.max_probe_candidates > 256
            or limits.timeout_seconds > 120
        ):
            raise ToolError(
                "One or more missing-media search limits exceed the safe maximum."
            )
        return limits


@dataclass
class _SearchState:
    deadline: float
    hashed_bytes: int = 0
    probe_count: int = 0
    hash_cache: dict[Path, str] = field(default_factory=dict)

    @property
    def timed_out(self) -> bool:
        return time.monotonic() >= self.deadline


def _integer(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ToolError(
            f"{name} must be an integer greater than or equal to {minimum}."
        )
    return value


def _authorized_roots(arguments: dict[str, Any]) -> list[Path]:
    roots_value = arguments.get("search_roots")
    if not isinstance(roots_value, list) or not 1 <= len(roots_value) <= 8:
        raise ToolError("search_roots must contain between 1 and 8 paths.")
    roots = [expand_path(value) for value in roots_value]
    if any(not root.is_dir() for root in roots):
        raise ToolError("Every search root must be an existing directory.")
    return roots


def _discover_files(
    roots: list[Path], limits: _SearchLimits, state: _SearchState
) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        for current, directories, names in os.walk(root, followlinks=False):
            if cancellation_requested():
                raise RequestCancelled("Missing-media diagnosis cancelled.")
            if state.timed_out or len(files) >= limits.max_files:
                break
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            if depth >= limits.max_depth:
                directories[:] = []
            for name in names:
                candidate = current_path / name
                try:
                    authorized = expand_path(str(candidate))
                except ToolError:
                    continue
                if authorized.is_file():
                    files.append(authorized)
                    if len(files) >= limits.max_files:
                        break
        if state.timed_out or len(files) >= limits.max_files:
            break
    return files


def _candidate_id(candidate: Path) -> str:
    return hashlib.sha256(str(candidate).encode()).hexdigest()[:16]


def _hash_matches(
    candidate: Path,
    expected_hash: object,
    limits: _SearchLimits,
    state: _SearchState,
) -> bool:
    if not expected_hash or state.hashed_bytes >= limits.max_hash_bytes:
        return False
    cost = min(candidate.stat().st_size, 2 * 1024 * 1024)
    if state.hashed_bytes + cost > limits.max_hash_bytes:
        return False
    digest = state.hash_cache.get(candidate)
    if digest is None:
        digest = shotcut_file_hash(candidate)
        state.hash_cache[candidate] = digest
        state.hashed_bytes += cost
    return digest.casefold() == str(expected_hash).casefold()


def _probe_candidate(
    candidate: Path,
    reference: dict[str, Any],
    limits: _SearchLimits,
    state: _SearchState,
) -> tuple[dict[str, Any] | None, int]:
    if state.probe_count >= limits.max_probe_candidates:
        return None, 0
    state.probe_count += 1
    try:
        media_summary = summarize_media(candidate)
    except ToolError as exc:
        return {"path": str(candidate), "error": str(exc)}, 0

    score = 0
    expected = reference.get("expected_media") or {}
    video = next(
        (
            stream
            for stream in media_summary["streams"]
            if stream.get("type") == "video"
        ),
        None,
    )
    expected_width = expected.get("width")
    expected_height = expected.get("height")
    if (
        video is not None
        and expected_width
        and expected_height
        and str(video.get("width")) == str(expected_width)
        and str(video.get("height")) == str(expected_height)
    ):
        score += 10
    expected_duration = expected.get("duration_seconds")
    actual_duration = media_summary.get("duration_seconds")
    if (
        isinstance(expected_duration, (int, float))
        and isinstance(actual_duration, (int, float))
        and abs(expected_duration - actual_duration)
        <= max(0.5, expected_duration * 0.05)
    ):
        score += 10
    return media_summary, score


def _rank_candidates(
    reference: dict[str, Any],
    files: list[Path],
    limits: _SearchLimits,
    state: _SearchState,
) -> list[dict[str, Any]]:
    expected_name = Path(reference["decoded_resource"]).name.casefold()
    expected_hash = reference.get("shotcut_hash")
    candidates: list[dict[str, Any]] = []
    for candidate in files:
        if state.timed_out:
            break
        basename_match = candidate.name.casefold() == expected_name
        hash_match = _hash_matches(candidate, expected_hash, limits, state)
        if not basename_match and not hash_match:
            continue
        media_summary, media_score = _probe_candidate(
            candidate, reference, limits, state
        )
        candidates.append(
            {
                "candidate_id": _candidate_id(candidate),
                "path": str(candidate),
                "score": 100 if hash_match else 60 + media_score,
                "match": "shotcut_hash" if hash_match else "basename",
                "verified": hash_match,
                "size_bytes": candidate.stat().st_size,
                "media": media_summary,
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["path"].casefold()))
    return candidates


def _visualize_candidates(
    resources: list[dict[str, Any]], arguments: dict[str, Any]
) -> dict[str, Any] | None:
    visual_output = arguments.get("visual_output_path")
    if visual_output is None:
        return None
    if not isinstance(visual_output, str):
        raise ToolError("visual_output_path must be a string.")
    selected: dict[str, Path] = {}
    for resource in resources:
        for candidate in resource["candidates"]:
            selected.setdefault(candidate["candidate_id"], Path(candidate["path"]))
            if len(selected) >= 64:
                break
    if not selected:
        return {"created": False, "error": "No candidates are available."}
    overwrite = arguments.get("overwrite_visual", False)
    if not isinstance(overwrite, bool):
        raise ToolError("overwrite_visual must be a boolean.")
    try:
        return render_media_contact_sheet(
            list(selected.items()),
            expand_path(visual_output),
            columns=_integer(arguments.get("visual_columns", 4), "visual_columns", 1),
            cell_width=_integer(
                arguments.get("visual_cell_width", 320), "visual_cell_width", 64
            ),
            overwrite=overwrite,
        )
    except RequestCancelled:
        raise
    except ToolError as exc:
        return {"created": False, "error": str(exc)}


def diagnose_missing_resources(
    resources: list[dict[str, Any]], arguments: dict[str, Any]
) -> dict[str, Any]:
    """Find bounded, ranked replacements for missing project resources."""

    roots = _authorized_roots(arguments)
    limits = _SearchLimits.from_arguments(arguments)
    state = _SearchState(time.monotonic() + limits.timeout_seconds)
    files = _discover_files(roots, limits, state)
    missing = [resource for resource in resources if resource.get("exists") is False]
    results: list[dict[str, Any]] = []
    for reference in missing:
        candidates = _rank_candidates(reference, files, limits, state)
        results.append(
            {
                "reference_id": reference["reference_id"],
                "missing_path": reference["resolved_path"],
                "stored_resource": reference["resource"],
                "candidates": candidates[: limits.max_candidates],
                "candidate_count": min(len(candidates), limits.max_candidates),
                "candidates_truncated": len(candidates) > limits.max_candidates,
            }
        )
    return {
        "missing_count": len(missing),
        "resources": results,
        "search": {
            "roots": [str(root) for root in roots],
            "files_examined": len(files),
            "files_limit_reached": len(files) >= limits.max_files,
            "hash_bytes_read": state.hashed_bytes,
            "media_probes": state.probe_count,
            "timed_out": state.timed_out,
        },
        "visual": _visualize_candidates(results, arguments),
    }
