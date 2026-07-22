"""Private filesystem storage for project locks and revision backups."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import ConflictError


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def project_lock(path: Path, stale_seconds: int = 600) -> Iterator[None]:
    """Hold an exclusive per-project lock without stealing it from a live process."""

    lock_path = path.with_suffix(path.suffix + ".shotcut-mcp.lock")
    payload = json.dumps({"pid": os.getpid(), "created_at": time.time()})
    for attempt in range(2):
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            break
        except FileExistsError:
            if attempt:
                raise ConflictError(
                    f"Another MCP process is editing the project: {lock_path}"
                )
            try:
                age = time.time() - lock_path.stat().st_mtime
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
                owner_pid = int(owner.get("pid", 0))
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                age = 0
                owner_pid = 0
            if age > stale_seconds and not _process_is_alive(owner_pid):
                lock_path.unlink(missing_ok=True)
                continue
            raise ConflictError(
                f"Another MCP process is editing the project: {lock_path}"
            )
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def backup_directory(project_path: Path) -> Path:
    canonical = os.path.normcase(str(project_path.resolve(strict=False)))
    identity = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return (
        project_path.parent
        / ".shotcut-mcp"
        / "backups"
        / f"{project_path.name}.{identity}"
    )


def _legacy_backup_pattern(project_path: Path) -> re.Pattern[str]:
    return re.compile(
        rf"^{re.escape(project_path.stem)}\."
        r"\d{8}T\d{6}\.\d{6}Z\."
        rf"[0-9a-f]{{12}}{re.escape(project_path.suffix)}$"
    )


def legacy_backup_directory(project_path: Path) -> Path:
    return project_path.parent / ".shotcut-mcp" / "backups"


def list_project_backups(project_path: Path) -> list[Path]:
    candidates: list[Path] = []
    isolated = backup_directory(project_path)
    if isolated.is_dir():
        candidates.extend(path for path in isolated.iterdir() if path.is_file())
    legacy = legacy_backup_directory(project_path)
    pattern = _legacy_backup_pattern(project_path)
    if legacy.is_dir():
        candidates.extend(
            path for path in legacy.iterdir() if path.is_file() and pattern.fullmatch(path.name)
        )
    return sorted(candidates, reverse=True)


def is_project_backup(project_path: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    isolated = backup_directory(project_path).resolve(strict=False)
    if resolved.parent == isolated:
        return resolved.is_file()
    legacy = legacy_backup_directory(project_path).resolve(strict=False)
    return (
        resolved.parent == legacy
        and resolved.is_file()
        and _legacy_backup_pattern(project_path).fullmatch(resolved.name) is not None
    )


def write_project_backup(project_path: Path, source: bytes, keep: int = 20) -> Path:
    directory = backup_directory(project_path)
    _private_directory(directory)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = directory / f"{stamp}.{_sha256(source)[:12]}{project_path.suffix}"
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(source)
        handle.flush()
        os.fsync(handle.fileno())
    for old in list_project_backups(project_path)[keep:]:
        if old.parent == directory:
            old.unlink(missing_ok=True)
    return backup_path
