from __future__ import annotations

import hashlib
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import render as render_module
from shotcut_mcp import render_jobs, render_worker
from shotcut_mcp.storage import OutputTransaction, RenderInputSnapshot


class RenderMonitoringTests(unittest.TestCase):
    def test_worker_bounds_memory_and_recovers_progress_after_an_oversized_line(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "renderer.py"
            source.write_text(
                "import sys\nfrom pathlib import Path\n"
                "sys.stdout.write('x' * (8 * 1024 * 1024))\n"
                "sys.stdout.write('\\nframe=99 percentage=99\\n')\n"
                "sys.stdout.flush()\n"
                "Path(next(a[9:] for a in sys.argv if a.startswith('avformat:'))).write_bytes(b'rendered')\n"
            )
            data = source.read_bytes()
            revision = hashlib.sha256(data).hexdigest()
            job_id = "a" * 32
            snapshot = RenderInputSnapshot.create(source, job_id, data, revision)
            output = OutputTransaction.prepare(root / "result.mp4", overwrite=False)
            with patch.object(render_jobs, "JOB_DIR", root / "jobs"):
                render_jobs.write_job(
                    {
                        "job_id": job_id,
                        "project_path": str(source),
                        "render_project_path": str(snapshot.path),
                        "editable_project_path": str(snapshot.path),
                        "project_revision": revision,
                        "output_transaction": output.serialize(),
                        "consumer_properties": {},
                        "melt_path": sys.executable,
                        "started_at": time.time(),
                        "total_frames": 100,
                    }
                )
                render_jobs.release_gate(job_id)
                tracemalloc.start()
                try:
                    self.assertEqual(render_worker.run_worker(job_id), 0)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                status = render_jobs.read_job(job_id)
                self.assertEqual(status["status"], "completed")
                self.assertEqual(status["current_frame"], 99)
                self.assertEqual(output.target.read_bytes(), b"rendered")
                self.assertLess(peak, 4 * 1024 * 1024)

    def test_render_log_is_bounded_and_supervisor_is_reaped_without_polling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            renderer = root / "noisy.py"
            output = root / "output.mp4"
            renderer.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "sys.stdout.write('x' * 800000)\n"
                "sys.stdout.flush()\n"
                "target = next(a[9:] for a in sys.argv if a.startswith('avformat:'))\n"
                "Path(target).write_bytes(b'rendered')\n",
                encoding="utf-8",
            )
            source = renderer.read_bytes()
            render_input = SimpleNamespace(
                source=source,
                revision=hashlib.sha256(source).hexdigest(),
                fps=30.0,
                duration_frames=100,
                markers=[],
            )
            with (
                patch(
                    "shotcut_mcp.render.load_project_render_input",
                    return_value=render_input,
                ),
                patch(
                    "shotcut_mcp.render.discover_executables",
                    return_value=SimpleNamespace(melt=Path(sys.executable)),
                ),
                patch(
                    "shotcut_mcp.render.require_executable",
                    return_value=Path(sys.executable),
                ),
                patch("shotcut_mcp.render.ensure_melt_ready"),
            ):
                job = render_module.start_render(
                    {
                        "project_path": str(renderer),
                        "output_path": str(output),
                    }
                )
            job_id = str(job["job_id"])
            worker = render_module.RUNNING_JOBS[job_id]
            worker.wait(timeout=15)
            time.sleep(1.2)
            self.assertNotIn(job_id, render_module.RUNNING_JOBS)
            status = render_module.render_status(job_id)
            self.assertEqual(status["status"], "completed")
            self.assertLessEqual(Path(status["log_path"]).stat().st_size, 512 * 1024)

    def test_history_is_paginated_and_running_job_has_eta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_dir = Path(directory) / "jobs"
            with patch.object(render_jobs, "JOB_DIR", job_dir):
                now = 10_000.0
                for index in range(3):
                    job_id = f"{index + 1:032x}"
                    render_jobs.write_job(
                        {
                            "job_id": job_id,
                            "status": "completed",
                            "project_path": "project.mlt",
                            "output_path": str(Path(directory) / f"{index}.mp4"),
                            "log_path": str(render_jobs.log_path(job_id)),
                            "started_at": now + index,
                            "finished_at": now + index + 1,
                        }
                    )
                first = render_module.list_render_jobs({"limit": 2})
                self.assertEqual(first["count"], 2)
                self.assertIsNotNone(first["next_cursor"])
                second = render_module.list_render_jobs(
                    {"limit": 2, "cursor": first["next_cursor"]}
                )
                self.assertEqual(second["count"], 1)

                active_id = "f" * 32
                render_jobs.write_job(
                    {
                        "job_id": active_id,
                        "status": "running",
                        "worker_pid": None,
                        "project_path": "project.mlt",
                        "output_path": str(Path(directory) / "active.mp4"),
                        "log_path": str(render_jobs.log_path(active_id)),
                        "started_at": time.time(),
                        "progress_percent": 20,
                        "progress_samples": [
                            {"at": time.time() - 5, "percent": 10, "frame": 10},
                            {"at": time.time(), "percent": 20, "frame": 20},
                        ],
                    }
                )
                status = render_module.render_status(active_id)
                self.assertIsNotNone(status["eta_seconds"])
                self.assertEqual(status["eta_basis"], "smoothed_progress_percent")


if __name__ == "__main__":
    unittest.main()
