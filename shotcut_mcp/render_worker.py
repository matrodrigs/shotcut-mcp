"""Out-of-process supervisor that owns one Melt render until final promotion."""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any, BinaryIO

from .errors import ToolError
from .platform import creation_flags, terminate_process
from .render_jobs import (
    cancel_requested,
    clear_control_files,
    gate_path,
    log_path,
    read_job,
    validate_job_id,
    write_job,
)
from .storage import OutputTransaction

MAX_LOG_BYTES = 512 * 1024
MAX_PROGRESS_SAMPLES = 32


def _stop_renderer(process: subprocess.Popen[Any]) -> None:
    terminate_process(process, grace_seconds=5)


def _command(metadata: dict[str, Any], output: OutputTransaction) -> list[str]:
    properties = metadata.get("consumer_properties")
    if not isinstance(properties, dict):
        raise ValueError("Invalid consumer properties in render metadata.")
    return [
        str(metadata["melt_path"]),
        str(metadata["project_path"]),
        "-progress2",
        "-consumer",
        f"avformat:{output.temporary}",
        "real_time=-1",
        "terminate_on_pause=1",
        *[f"{key}={value}" for key, value in properties.items()],
    ]


class _BoundedLog:
    def __init__(self, path: str) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
        self.handle: BinaryIO = os.fdopen(descriptor, "w+b", buffering=0)

    def append(self, text: str) -> None:
        self.handle.write(text.encode("utf-8", errors="replace"))
        if self.handle.tell() <= MAX_LOG_BYTES:
            return
        keep = MAX_LOG_BYTES // 2
        self.handle.seek(-keep, os.SEEK_END)
        tail = self.handle.read()
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(b"[earlier render log truncated]\n" + tail)

    def close(self) -> None:
        self.handle.close()


def _read_chunks(stream: Any, messages: queue.Queue[bytes | None]) -> None:
    try:
        for chunk in iter(lambda: stream.read(8192), b""):
            messages.put(chunk)
    finally:
        messages.put(None)


def _observe_progress(metadata: dict[str, Any], line: str) -> bool:
    percent_match = re.search(
        r"(?:percentage|percent|progress)\s*[:=]\s*(\d{1,3})", line, re.I
    )
    frame_match = re.search(r"(?:current\s+)?frame\s*[:=]\s*(\d+)", line, re.I)
    if not percent_match and not frame_match:
        return False
    changed = False
    if percent_match:
        percent = min(100, int(percent_match.group(1)))
        if metadata.get("progress_percent") != percent:
            metadata["progress_percent"] = percent
            changed = True
    if frame_match:
        frame = int(frame_match.group(1))
        if metadata.get("current_frame") != frame:
            metadata["current_frame"] = frame
            changed = True
    if changed:
        now = time.time()
        samples = deque(
            metadata.get("progress_samples") or [], maxlen=MAX_PROGRESS_SAMPLES
        )
        samples.append(
            {
                "at": now,
                "percent": metadata.get("progress_percent"),
                "frame": metadata.get("current_frame"),
            }
        )
        metadata["progress_samples"] = list(samples)
        metadata["updated_at"] = now
    return changed


def run_worker(job_id: str) -> int:
    validate_job_id(job_id)
    deadline = time.monotonic() + 5
    while not gate_path(job_id).is_file() and time.monotonic() < deadline:
        if cancel_requested(job_id):
            break
        time.sleep(0.05)

    metadata = read_job(job_id)
    output = OutputTransaction.deserialize(metadata.get("output_transaction"))
    metadata.update(worker_pid=os.getpid(), status="running")
    write_job(metadata)
    process: subprocess.Popen[Any] | None = None
    try:
        if cancel_requested(job_id):
            metadata.update(
                status="cancelled", return_code=None, finished_at=time.time()
            )
            output.cleanup()
            write_job(metadata)
            return 0

        path = log_path(job_id)
        log = _BoundedLog(str(path))
        try:
            process = subprocess.Popen(
                _command(metadata, output),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags(),
                start_new_session=os.name != "nt",
            )
            metadata.update(renderer_pid=process.pid, status="running")
            write_job(metadata)
            messages: queue.Queue[bytes | None] = queue.Queue(maxsize=128)
            reader = threading.Thread(
                target=_read_chunks, args=(process.stdout, messages), daemon=True
            )
            reader.start()
            reader_finished = False
            pending_progress = ""
            last_write = time.monotonic()
            while process.poll() is None or not reader_finished:
                if cancel_requested(job_id):
                    _stop_renderer(process)
                try:
                    chunk = messages.get(timeout=0.1)
                except queue.Empty:
                    chunk = (
                        None
                        if process.poll() is not None and not reader.is_alive()
                        else b""
                    )
                if chunk is None:
                    reader_finished = True
                elif chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    log.append(text)
                    pending_progress += text
                    lines = pending_progress.splitlines(keepends=True)
                    pending_progress = ""
                    if lines and not lines[-1].endswith(("\n", "\r")):
                        pending_progress = lines.pop()
                    for line in lines:
                        _observe_progress(metadata, line)
                if time.monotonic() - last_write >= 0.5:
                    metadata["updated_at"] = time.time()
                    write_job(metadata)
                    last_write = time.monotonic()
            reader.join(timeout=1)
            if pending_progress:
                _observe_progress(metadata, pending_progress)
            process.wait()
        finally:
            log.close()

        return_code = process.returncode
        finished_at = time.time()
        started_at = float(metadata.get("started_at") or finished_at)
        elapsed = max(0.0, finished_at - started_at)
        current_frame = metadata.get("current_frame")
        metadata.update(
            return_code=return_code,
            finished_at=finished_at,
            updated_at=finished_at,
            elapsed_seconds=elapsed,
            average_fps=(
                current_frame / elapsed
                if isinstance(current_frame, int) and current_frame > 0 and elapsed > 0
                else None
            ),
        )
        if cancel_requested(job_id):
            metadata["status"] = "cancelled"
            output.cleanup()
        elif return_code != 0 or not output.temporary.is_file():
            metadata["status"] = "failed"
            metadata["status_note"] = (
                f"Melt exited with code {return_code}; no output was promoted."
            )
            output.cleanup()
        else:
            try:
                output.commit()
            except (OSError, ToolError) as exc:
                metadata["status"] = "promotion_failed"
                metadata["status_note"] = (
                    "Rendering completed, but atomic promotion failed; the temporary "
                    f"file was retained at {output.temporary}: {exc}"
                )
            else:
                metadata["status"] = "completed"
                metadata["progress_percent"] = 100
                metadata["output_size_bytes"] = output.target.stat().st_size
        write_job(metadata)
        return 0 if metadata["status"] == "completed" else 1
    except Exception as exc:
        if process is not None:
            _stop_renderer(process)
        output.cleanup()
        metadata.update(
            status="failed",
            status_note=f"Render supervisor failed: {exc}",
            finished_at=time.time(),
        )
        write_job(metadata)
        return 1
    finally:
        clear_control_files(job_id)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        return run_worker(sys.argv[1])
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
