"""Out-of-process supervisor that owns one Melt render until final promotion."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any

from .errors import ToolError
from .platform import creation_flags
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


def _stop_renderer(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _command(metadata: dict[str, Any], output: OutputTransaction) -> list[str]:
    properties = metadata.get("consumer_properties")
    if not isinstance(properties, dict):
        raise ValueError("Invalid consumer properties in render metadata.")
    return [
        str(metadata["melt_path"]),
        str(metadata["project_path"]),
        "-progress",
        "-consumer",
        f"avformat:{output.temporary}",
        "real_time=-1",
        "terminate_on_pause=1",
        *[f"{key}={value}" for key, value in properties.items()],
    ]


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
        descriptor = os.open(
            path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                _command(metadata, output),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creation_flags(),
                start_new_session=os.name != "nt",
            )
            metadata.update(renderer_pid=process.pid, status="running")
            write_job(metadata)
            while process.poll() is None:
                if cancel_requested(job_id):
                    _stop_renderer(process)
                    break
                time.sleep(0.1)

        return_code = process.returncode
        metadata.update(return_code=return_code, finished_at=time.time())
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
