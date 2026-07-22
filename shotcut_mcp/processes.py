"""Executable discovery and cancellable subprocess management."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RequestCancelled, ToolError
from .path_policy import expand_path
from .protocol import cancellation_requested

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


def _which(name: str) -> Path | None:
    value = shutil.which(name)
    return Path(value).resolve() if value else None


def _first_existing(candidates: list[Path | None]) -> Path | None:
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def sys_platform() -> str:
    import sys

    return sys.platform


def discover_executables() -> Executables:
    """Discover a coherent Shotcut/MLT/FFmpeg toolchain."""

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


def runtime_identity(executable: Path) -> tuple[object, ...]:
    """Identify an executable and the MLT environment that controls its behavior."""

    try:
        stat = executable.stat()
    except OSError as exc:
        raise ToolError(f"Could not inspect executable {executable}: {exc}") from exc
    environment = tuple((key, os.environ.get(key)) for key in MLT_ENVIRONMENT_KEYS)
    return (str(executable), stat.st_mtime_ns, stat.st_size, environment)


def run_capture(
    command: list[str], timeout: int = 30, max_output_bytes: int = 4 * 1024 * 1024
) -> subprocess.CompletedProcess[str]:
    """Run a child with bounded time, output, and cancellation propagation."""

    if cancellation_requested():
        raise RequestCancelled("Request cancelled before the command started.")
    if max_output_bytes < 1024:
        raise ValueError("max_output_bytes must be at least 1024.")
    with (
        tempfile.TemporaryFile("w+b") as stdout_file,
        tempfile.TemporaryFile("w+b") as stderr_file,
    ):
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=creation_flags(),
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise ToolError(f"Could not run {command[0]}: {exc}") from exc
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancellation_requested():
                terminate_process(process)
                raise RequestCancelled("Request cancelled by the MCP client.")
            if (
                os.fstat(stdout_file.fileno()).st_size > max_output_bytes
                or os.fstat(stderr_file.fileno()).st_size > max_output_bytes
            ):
                terminate_process(process)
                raise ToolError(
                    f"The command exceeded the {max_output_bytes}-byte output limit."
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process(process)
                expired = subprocess.TimeoutExpired(command, timeout)
                raise ToolError(
                    f"The command timed out after {timeout} seconds."
                ) from expired
            time.sleep(min(0.05, remaining))
        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        if stdout_size > max_output_bytes or stderr_size > max_output_bytes:
            raise ToolError(
                f"The command exceeded the {max_output_bytes}-byte output limit."
            )
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def terminate_process(process: subprocess.Popen[Any], grace_seconds: float = 2) -> None:
    """Terminate a child process group, escalating after a grace period."""

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
