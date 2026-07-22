"""Private filesystem storage for project locks and revision backups."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConflictError, ToolError


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def paths_refer_to_same_file(first: Path, second: Path) -> bool:
    if os.path.normcase(str(first.resolve(strict=False))) == os.path.normcase(
        str(second.resolve(strict=False))
    ):
        return True
    try:
        return first.samefile(second)
    except (FileNotFoundError, OSError):
        return False


@dataclass(frozen=True)
class OutputTransaction:
    """Render into a sibling temporary file and atomically promote it when safe."""

    target: Path
    temporary: Path
    initial_signature: tuple[int, int, int, int] | None
    initial_mode: int | None

    @classmethod
    def prepare(
        cls,
        target: Path,
        *,
        overwrite: bool,
        protected_paths: tuple[Path, ...] = (),
    ) -> OutputTransaction:
        for protected in protected_paths:
            if paths_refer_to_same_file(target, protected):
                raise ToolError(
                    f"Output must not refer to the protected input file: {protected}"
                )
        signature = _file_signature(target)
        if signature is not None and not target.is_file():
            raise ToolError(f"The existing output is not a file: {target}")
        if signature is not None and not overwrite:
            raise ToolError(f"The output already exists: {target}")
        mode = target.stat().st_mode if signature is not None else None
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(
            f".{target.stem}.{uuid.uuid4().hex}.tmp{target.suffix}"
        )
        return cls(target, temporary, signature, mode)

    def commit(self) -> None:
        if not self.temporary.is_file():
            raise ToolError("The renderer did not create its temporary output.")
        if _file_signature(self.target) != self.initial_signature:
            raise ConflictError(
                f"The output changed while rendering and was not replaced: {self.target}"
            )
        with self.temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        if self.initial_mode is not None:
            os.chmod(self.temporary, self.initial_mode)
        os.replace(self.temporary, self.target)
        fsync_directory(self.target.parent)

    def cleanup(self) -> None:
        self.temporary.unlink(missing_ok=True)

    def serialize(self) -> dict[str, object]:
        return {
            "target": str(self.target),
            "temporary": str(self.temporary),
            "initial_signature": (
                list(self.initial_signature)
                if self.initial_signature is not None
                else None
            ),
            "initial_mode": self.initial_mode,
        }

    @classmethod
    def deserialize(cls, value: object) -> OutputTransaction:
        if not isinstance(value, dict):
            raise ToolError("Invalid output transaction metadata.")
        target = value.get("target")
        temporary = value.get("temporary")
        signature = value.get("initial_signature")
        mode = value.get("initial_mode")
        if not isinstance(target, str) or not isinstance(temporary, str):
            raise ToolError("Invalid output transaction paths.")
        if signature is not None and (
            not isinstance(signature, list)
            or len(signature) != 4
            or not all(isinstance(item, int) for item in signature)
        ):
            raise ToolError("Invalid output transaction signature.")
        if mode is not None and not isinstance(mode, int):
            raise ToolError("Invalid output transaction mode.")
        return cls(
            Path(target),
            Path(temporary),
            tuple(signature) if signature is not None else None,
            mode,
        )


@contextmanager
def project_lock(path: Path, stale_seconds: int = 600) -> Iterator[None]:
    """Hold an exclusive per-project lock without stealing it from a live process."""

    lock_path = path.with_suffix(path.suffix + ".shotcut-mcp.lock")
    payload = json.dumps({"pid": os.getpid(), "created_at": time.time()})
    for attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            break
        except FileExistsError:
            if attempt:
                raise ConflictError(
                    f"Another MCP process is editing the project: {lock_path}"
                ) from None
            try:
                age = time.time() - lock_path.stat().st_mtime
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
                owner_pid = int(owner.get("pid", 0))
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                age = 0
                owner_pid = 0
            if age > stale_seconds and not process_is_alive(owner_pid):
                lock_path.unlink(missing_ok=True)
                continue
            raise ConflictError(
                f"Another MCP process is editing the project: {lock_path}"
            ) from None
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


def _isolated_backup_pattern(project_path: Path) -> re.Pattern[str]:
    return re.compile(
        r"^\d{8}T\d{6}\.\d{6}Z\."
        rf"[0-9a-f]{{12}}{re.escape(project_path.suffix)}$"
    )


def legacy_backup_directory(project_path: Path) -> Path:
    return project_path.parent / ".shotcut-mcp" / "backups"


def list_project_backups(project_path: Path) -> list[Path]:
    candidates: list[Path] = []
    isolated = backup_directory(project_path)
    isolated_pattern = _isolated_backup_pattern(project_path)
    if isolated.is_dir():
        candidates.extend(
            path
            for path in isolated.iterdir()
            if path.is_file() and isolated_pattern.fullmatch(path.name)
        )
    legacy = legacy_backup_directory(project_path)
    pattern = _legacy_backup_pattern(project_path)
    if legacy.is_dir():
        candidates.extend(
            path
            for path in legacy.iterdir()
            if path.is_file() and pattern.fullmatch(path.name)
        )
    return sorted(candidates, reverse=True)


def is_project_backup(project_path: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    isolated = backup_directory(project_path).resolve(strict=False)
    if resolved.parent == isolated:
        return (
            resolved.is_file()
            and _isolated_backup_pattern(project_path).fullmatch(resolved.name)
            is not None
        )
    legacy = legacy_backup_directory(project_path).resolve(strict=False)
    return (
        resolved.parent == legacy
        and resolved.is_file()
        and _legacy_backup_pattern(project_path).fullmatch(resolved.name) is not None
    )


def write_project_backup(project_path: Path, source: bytes, keep: int = 20) -> Path:
    directory = backup_directory(project_path)
    _private_directory(directory)
    try:
        directory.resolve(strict=True).relative_to(
            project_path.parent.resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise ToolError(
            "The backup directory resolves outside the project directory."
        ) from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = directory / f"{stamp}.{_sha256(source)[:12]}{project_path.suffix}"
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(source)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(directory)
    isolated_backups = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and _isolated_backup_pattern(project_path).fullmatch(path.name)
        ),
        reverse=True,
    )
    for old in isolated_backups[keep:]:
        old.unlink(missing_ok=True)
    return backup_path
