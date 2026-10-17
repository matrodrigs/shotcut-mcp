from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import render, render_jobs, render_worker
from shotcut_mcp.errors import RequestCancelled, ToolError
from shotcut_mcp.protocol import request_cancellation


class RenderAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project.mlt"
        self.project.write_bytes(b"<mlt />")
        self.job_directory = self.root / "jobs"
        render_input = SimpleNamespace(
            source=self.project.read_bytes(),
            revision=hashlib.sha256(self.project.read_bytes()).hexdigest(),
            fps=30.0,
            duration_frames=100,
            markers=[],
        )
        patches = [
            patch.object(render_jobs, "JOB_DIR", self.job_directory),
            patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": "2"}),
            patch.object(
                render, "load_project_render_input", return_value=render_input
            ),
            patch.object(
                render,
                "discover_executables",
                return_value=SimpleNamespace(melt=Path("melt")),
            ),
            patch.object(render, "require_executable", return_value=Path("melt")),
            patch.object(render, "ensure_melt_ready"),
            patch.object(
                render,
                "start_render_supervisor",
                return_value=SimpleNamespace(pid=os.getpid()),
            ),
            patch.object(
                render,
                "threading",
                SimpleNamespace(
                    Thread=lambda **_options: SimpleNamespace(start=lambda: None)
                ),
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(render.RUNNING_JOBS.clear)

    def _start(self) -> render_jobs.RenderJob:
        return render.start_render(
            {
                "project_path": str(self.project),
                "output_path": str(self.root / "output.mp4"),
            }
        )

    def _active(self, index: int = 1, **fields: object) -> str:
        job_id = f"{index:032x}"
        render_jobs.write_job(
            {
                "job_id": job_id,
                "status": "running",
                "worker_pid": os.getpid(),
                "started_at": time.time(),
                **fields,
            }
        )
        return job_id

    def test_default_limit_uses_persisted_jobs_even_after_local_state_is_lost(
        self,
    ) -> None:
        first = self._start()
        second = self._start()
        render.RUNNING_JOBS.clear()
        with self.assertRaises(ToolError) as caught:
            self._start()
        self.assertEqual(caught.exception.code, "render_capacity_reached")
        self.assertEqual(caught.exception.recommended_tool, "list_render_jobs")
        self.assertEqual(
            set(caught.exception.details["active_job_ids"]),
            {first["job_id"], second["job_id"]},
        )
        self.assertEqual(len(list(self.root.glob("*.render-*.mlt"))), 2)

    def test_capacity_is_visible_to_an_independent_server_process(self) -> None:
        self._active(1)
        self._active(2)
        program = f"""
import json
from pathlib import Path
from shotcut_mcp import render_jobs
from shotcut_mcp.errors import ToolError
from tests.test_rendering import RenderLifecycleTests
render_jobs.JOB_DIR = Path({str(self.job_directory)!r})
try:
    RenderLifecycleTests._start_with_python_renderer(Path({str(self.project)!r}), Path({str(self.root / "other.mp4")!r}))
except ToolError as exc:
    print(json.dumps({{'code': exc.code, 'details': exc.details}}))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", program],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "render_capacity_reached")

    def test_concurrent_servers_cannot_start_during_admission(self) -> None:
        ready = self.root / "ready"
        proceed = self.root / "proceed"
        program = f"""
import os, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from shotcut_mcp import render, render_jobs
from tests.test_rendering import RenderLifecycleTests
render_jobs.JOB_DIR = Path({str(self.job_directory)!r})
def start(*_args):
    Path({str(ready)!r}).touch()
    deadline = time.monotonic() + 10
    while not Path({str(proceed)!r}).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('admission test timed out')
        time.sleep(0.01)
    return SimpleNamespace(pid=os.getpid())
with patch.object(render, 'start_render_supervisor', side_effect=start), patch.object(render, 'threading', SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: None))):
    RenderLifecycleTests._start_with_python_renderer(Path({str(self.project)!r}), Path({str(self.root / "other.mp4")!r}))
"""
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", program],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while (
                not ready.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            with self.assertRaises(ToolError) as caught:
                self._start()
            self.assertEqual(caught.exception.code, "render_admission_busy")
        finally:
            proceed.touch()
            stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self._start()["status"], "running")

    def test_queued_and_live_orphaned_jobs_count_but_dead_jobs_do_not(self) -> None:
        for fields in (
            {"status": "queued", "worker_pid": None},
            {"status": "orphaned", "worker_pid": None, "renderer_pid": os.getpid()},
            {"status": "running", "worker_pid": 2**30, "renderer_pid": os.getpid()},
        ):
            with (
                self.subTest(fields=fields),
                patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": "1"}),
            ):
                self._active(**fields)
                with self.assertRaises(ToolError) as caught:
                    self._start()
                self.assertEqual(caught.exception.code, "render_capacity_reached")
        self._active(status="running", worker_pid=2**30)
        self._active(2, status="queued", worker_pid=None, started_at=time.time() - 60)
        self.assertEqual(self._start()["status"], "running")

    def test_process_death_releases_admission_without_a_stale_lock_timeout(
        self,
    ) -> None:
        ready = self.root / "owner-ready"
        program = f"""
import time
from pathlib import Path
from shotcut_mcp import render_jobs
render_jobs.JOB_DIR = Path({str(self.job_directory)!r})
with render_jobs.render_admission():
    Path({str(ready)!r}).touch()
    time.sleep(30)
"""
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", program],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while (
                not ready.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            with self.assertRaises(ToolError) as caught:
                self._start()
            self.assertEqual(caught.exception.code, "render_admission_busy")
        finally:
            process.kill()
            process.communicate(timeout=10)
        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": "1"}):
            self.assertEqual(self._start()["status"], "running")

    def test_configuration_is_clamped_and_invalid_values_use_default(self) -> None:
        for value, maximum in (("0", 1), ("900", 8), ("invalid", 2)):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": value}),
            ):
                for index in range(1, maximum + 1):
                    self._active(index)
                with self.assertRaises(ToolError) as caught:
                    self._start()
                self.assertEqual(
                    caught.exception.details["maximum_active_renders"], maximum
                )

    def test_history_limit_is_checked_before_cleanup_or_render_preparation(
        self,
    ) -> None:
        old_job = self._active(status="completed")
        old_metadata = render_jobs.metadata_path(old_job)
        old = time.time() - 60 * 86_400
        os.utime(old_metadata, (old, old))
        history = [old_metadata] + [
            self.job_directory / f"{index:032x}.json" for index in range(2, 5002)
        ]
        with (
            patch.object(Path, "glob", side_effect=lambda *_args: iter(history)),
            self.assertRaises(ToolError) as caught,
        ):
            self._start()
        self.assertEqual(caught.exception.code, "render_history_limit_reached")
        self.assertTrue(old_metadata.is_file())
        self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])

    def test_start_failure_releases_capacity_and_preserves_existing_output(
        self,
    ) -> None:
        output = self.root / "output.mp4"
        output.write_bytes(b"existing")
        with (
            patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": "1"}),
            patch.object(
                render, "start_render_supervisor", side_effect=OSError("cannot spawn")
            ),
            self.assertRaises(ToolError) as caught,
        ):
            render.start_render(
                {
                    "project_path": str(self.project),
                    "output_path": str(output),
                    "overwrite": True,
                }
            )
        self.assertEqual(caught.exception.code, "render_start_failed")
        self.assertEqual(output.read_bytes(), b"existing")
        self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])
        result = render.start_render(
            {
                "project_path": str(self.project),
                "output_path": str(output),
                "overwrite": True,
            }
        )
        self.assertEqual(result["status"], "running")

    def test_metadata_failure_after_spawn_stops_supervisor_and_releases_capacity(
        self,
    ) -> None:
        def write(metadata: dict[str, object]) -> None:
            if metadata.get("status") == "running":
                raise OSError("metadata unavailable")
            render_jobs.write_job(metadata)

        with (
            patch.object(render, "write_job", side_effect=write),
            patch.object(render, "terminate_process") as terminate,
            self.assertRaises(ToolError) as caught,
        ):
            self._start()
        self.assertEqual(caught.exception.code, "render_start_failed")
        terminate.assert_called_once()
        self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])
        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": "1"}):
            self.assertEqual(self._start()["status"], "running")

    def test_cancelled_start_releases_capacity_without_spawning(self) -> None:
        event = threading.Event()
        event.set()
        with (
            request_cancellation(event),
            patch.object(render, "start_render_supervisor") as spawn,
            self.assertRaises(RequestCancelled),
        ):
            self._start()
        spawn.assert_not_called()
        jobs = render.list_render_jobs({})["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "cancelled")
        self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])
        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": "1"}):
            self.assertEqual(self._start()["status"], "running")

    def test_live_orphaned_history_is_not_pruned_out_of_capacity(self) -> None:
        job_id = self._active(
            status="orphaned", worker_pid=None, renderer_pid=os.getpid()
        )
        old = time.time() - 60 * 86_400
        os.utime(render_jobs.metadata_path(job_id), (old, old))
        with (
            patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": "1"}),
            self.assertRaises(ToolError) as caught,
        ):
            self._start()
        self.assertEqual(caught.exception.code, "render_capacity_reached")
        self.assertTrue(render_jobs.metadata_path(job_id).is_file())

    def test_worker_without_parent_confirmation_cleans_job_without_rendering(
        self,
    ) -> None:
        with patch.object(render, "release_gate"):
            job = self._start()
        ticks = iter((0, 6))
        with (
            patch.object(
                render_worker,
                "time",
                SimpleNamespace(monotonic=lambda: next(ticks), time=time.time),
            ),
            patch.object(render_worker.subprocess, "Popen") as spawn,
        ):
            self.assertEqual(render_worker.run_worker(job["job_id"]), 1)
        spawn.assert_not_called()
        self.assertEqual(render_jobs.read_job(job["job_id"])["status"], "failed")
        self.assertFalse(Path(job["render_project_path"]).exists())
        self.assertFalse(Path(job["output_path"]).exists())
        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_ACTIVE_RENDERS": "1"}):
            self.assertEqual(self._start()["status"], "running")
