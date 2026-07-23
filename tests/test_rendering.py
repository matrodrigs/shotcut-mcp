from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import render as render_module
from shotcut_mcp import render_jobs as render_jobs_module
from shotcut_mcp import render_worker as render_worker_module
from shotcut_mcp.errors import ToolError
from shotcut_mcp.platform import render_preview
from shotcut_mcp.render import cancel_render, render_status, start_render
from shotcut_mcp.storage import OutputTransaction


class PreviewSafetyTests(unittest.TestCase):
    @staticmethod
    def _platform_patches(render: object) -> tuple[object, ...]:
        return (
            patch(
                "shotcut_mcp.platform.discover_executables",
                return_value=SimpleNamespace(melt=Path("melt")),
            ),
            patch("shotcut_mcp.platform.require_executable", return_value=Path("melt")),
            patch("shotcut_mcp.platform.ensure_melt_ready"),
            patch("shotcut_mcp.platform.run_capture", side_effect=render),
        )

    def test_preview_rejects_the_project_as_its_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            original = b"<mlt/>\n"
            project_path.write_bytes(original)

            def render(command: list[str], **_kwargs: object) -> SimpleNamespace:
                target = next(
                    value.removeprefix("avformat:")
                    for value in command
                    if value.startswith("avformat:")
                )
                Path(target).write_bytes(b"PNG")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            first, second, third, fourth = self._platform_patches(render)
            with first, second, third, fourth, self.assertRaises(ToolError):
                render_preview(project_path, project_path, frame=0, overwrite=True)

            self.assertEqual(project_path.read_bytes(), original)

    def test_failed_preview_preserves_an_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            output_path = Path(directory) / "preview.png"
            project_path.write_text("<mlt/>\n", encoding="utf-8")
            output_path.write_bytes(b"existing")

            def fail_after_partial_output(
                command: list[str], **_kwargs: object
            ) -> SimpleNamespace:
                target = next(
                    value.removeprefix("avformat:")
                    for value in command
                    if value.startswith("avformat:")
                )
                Path(target).write_bytes(b"partial")
                return SimpleNamespace(returncode=1, stdout="", stderr="failed")

            first, second, third, fourth = self._platform_patches(
                fail_after_partial_output
            )
            with first, second, third, fourth, self.assertRaises(ToolError):
                render_preview(project_path, output_path, frame=0, overwrite=True)

            self.assertEqual(output_path.read_bytes(), b"existing")
            self.assertEqual(list(Path(directory).glob("*.tmp.png")), [])

    def test_managed_preview_uses_one_bounded_server_owned_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            project_path.write_text("<mlt/>\n", encoding="utf-8")

            def render(command: list[str], **_kwargs: object) -> SimpleNamespace:
                target = next(
                    value.removeprefix("avformat:")
                    for value in command
                    if value.startswith("avformat:")
                )
                Path(target).write_bytes(b"PNG")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            first, second, third, fourth = self._platform_patches(render)
            with first, second, third, fourth:
                first_result = render_preview(
                    project_path, None, frame=0, overwrite=False
                )
                second_result = render_preview(
                    project_path, None, frame=30, overwrite=False
                )

            self.assertTrue(first_result["managed_output"])
            self.assertEqual(first_result["path"], second_result["path"])
            self.assertEqual(Path(first_result["path"]).read_bytes(), b"PNG")
            previews = list(
                (Path(directory) / ".shotcut-mcp" / "previews").rglob("*.png")
            )
            self.assertEqual(previews, [Path(first_result["path"])])


class RenderPropertySafetyTests(unittest.TestCase):
    def test_sidecar_consumer_properties_are_rejected_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            output_path = Path(directory) / "output.mp4"
            escaped_path = Path(directory) / "escaped-%03d.ts"
            project_path.write_text("<mlt/>\n", encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "safe allowlist"):
                start_render(
                    {
                        "project_path": str(project_path),
                        "output_path": str(output_path),
                        "consumer_properties": {
                            "f": "hls",
                            "hls_segment_filename": str(escaped_path),
                        },
                    }
                )


class RenderJobPersistenceTests(unittest.TestCase):
    def test_cancel_render_retries_transient_metadata_read_contention(self) -> None:
        class SharingViolation(PermissionError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            job_directory = Path(directory) / "jobs"
            job_id = "c" * 32
            metadata = {
                "job_id": job_id,
                "status": "cancelled",
                "output_path": str(Path(directory) / "output.mp4"),
                "log_path": str(Path(directory) / "render.log"),
                "started_at": time.time(),
                "finished_at": time.time(),
            }

            with (
                patch.object(render_jobs_module, "JOB_DIR", job_directory),
                patch.object(render_jobs_module, "_IS_WINDOWS", True),
            ):
                render_jobs_module.write_job(metadata)
                path = render_jobs_module.metadata_path(job_id)
                path_type = type(path)
                real_read_text = path_type.read_text
                attempts = 0

                def read_after_contention(
                    candidate: Path, *args: object, **kwargs: object
                ) -> str:
                    nonlocal attempts
                    if candidate == path:
                        attempts += 1
                        if attempts < 3:
                            raise SharingViolation(
                                13, "simulated metadata read contention"
                            )
                    return real_read_text(candidate, *args, **kwargs)

                with patch.object(path_type, "read_text", new=read_after_contention):
                    result = cancel_render(job_id)

            self.assertEqual(result["status"], "cancelled")
            self.assertGreaterEqual(attempts, 3)

    def test_job_state_retries_a_windows_sharing_violation(self) -> None:
        class SharingViolation(PermissionError):
            winerror = 32

        with tempfile.TemporaryDirectory() as directory:
            job_directory = Path(directory) / "jobs"
            job_id = "a" * 32
            metadata = {"job_id": job_id, "status": "queued"}
            real_replace = os.replace
            attempts = 0

            def replace_after_contention(source: object, target: object) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise SharingViolation(13, "simulated sharing violation")
                real_replace(source, target)

            with (
                patch.object(render_jobs_module, "JOB_DIR", job_directory),
                patch.object(
                    render_jobs_module.os,
                    "replace",
                    side_effect=replace_after_contention,
                ),
            ):
                render_jobs_module.write_job(metadata)
                stored = render_jobs_module.read_job(job_id)

            self.assertEqual(stored, metadata)
            self.assertEqual(attempts, 3)
            self.assertEqual(list(job_directory.glob(".*.tmp")), [])

    def test_worker_main_records_an_unhandled_initialization_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_directory = Path(directory) / "jobs"
            output = OutputTransaction.prepare(
                Path(directory) / "output.mp4", overwrite=False
            )
            output.temporary.write_bytes(b"partial")
            job_id = "b" * 32
            metadata = {
                "job_id": job_id,
                "status": "running",
                "output_transaction": output.serialize(),
            }

            with patch.object(render_jobs_module, "JOB_DIR", job_directory):
                render_jobs_module.write_job(metadata)
                render_jobs_module.release_gate(job_id)
                with (
                    patch.object(
                        render_worker_module,
                        "run_worker",
                        side_effect=RuntimeError("simulated startup failure"),
                    ),
                    patch.object(sys, "argv", ["render_worker", job_id]),
                ):
                    return_code = render_worker_module.main()
                stored = render_jobs_module.read_job(job_id)

                self.assertFalse(render_jobs_module.gate_path(job_id).exists())

            self.assertEqual(return_code, 1)
            self.assertEqual(stored["status"], "failed")
            self.assertIn("simulated startup failure", stored["status_note"])
            self.assertFalse(output.temporary.exists())


class RenderLifecycleTests(unittest.TestCase):
    @staticmethod
    def _wait_for_status(
        job_id: str, expected: str, timeout: float = 15
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        latest = render_status(job_id)
        while (
            latest["status"] != expected
            and latest["status"] not in render_jobs_module.TERMINAL_STATUSES
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            latest = render_status(job_id)
        return latest

    @staticmethod
    def _start_with_python_renderer(
        renderer_path: Path, output_path: Path
    ) -> dict[str, object]:
        with (
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
            return start_render(
                {
                    "project_path": str(renderer_path),
                    "output_path": str(output_path),
                }
            )

    def test_completed_render_is_promoted_without_status_polling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer_path = Path(directory) / "renderer.py"
            output_path = Path(directory) / "output.mp4"
            renderer_path.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "target = next(a[9:] for a in sys.argv if a.startswith('avformat:'))\n"
                "Path(target).write_bytes(b'rendered')\n"
                "print('percentage: 100', flush=True)\n",
                encoding="utf-8",
            )

            job = self._start_with_python_renderer(renderer_path, output_path)
            worker = render_module.RUNNING_JOBS[str(job["job_id"])]

            try:
                worker.wait(timeout=15)
                self.assertTrue(
                    output_path.is_file(),
                    "the worker must promote output without a render_status request",
                )
            finally:
                if worker.poll() is None:
                    worker.wait(timeout=15)
                render_status(str(job["job_id"]))

    def test_render_remains_managed_after_session_state_is_lost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer_path = Path(directory) / "slow_renderer.py"
            output_path = Path(directory) / "output.mp4"
            renderer_path.write_text(
                "from pathlib import Path\n"
                "import sys, time\n"
                "time.sleep(0.5)\n"
                "target = next(a[9:] for a in sys.argv if a.startswith('avformat:'))\n"
                "Path(target).write_bytes(b'rendered')\n",
                encoding="utf-8",
            )
            job = self._start_with_python_renderer(renderer_path, output_path)
            worker = render_module.RUNNING_JOBS.pop(str(job["job_id"]))
            try:
                active = render_status(str(job["job_id"]))
                self.assertIn(active["status"], {"queued", "running", "completed"})

                status = self._wait_for_status(str(job["job_id"]), "completed")
                self.assertTrue(output_path.is_file())
                self.assertEqual(status["status"], "completed")
            finally:
                if worker.poll() is None:
                    worker.wait(timeout=15)

    def test_render_can_be_cancelled_after_session_state_is_lost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer_path = Path(directory) / "slow_renderer.py"
            output_path = Path(directory) / "output.mp4"
            renderer_path.write_text(
                "import time\ntime.sleep(20)\n",
                encoding="utf-8",
            )
            job = self._start_with_python_renderer(renderer_path, output_path)
            worker = render_module.RUNNING_JOBS.pop(str(job["job_id"]))

            try:
                requested = cancel_render(str(job["job_id"]))
                if requested["status"] != "cancelled":
                    self.assertTrue(requested.get("cancellation_requested"))
                cancelled = self._wait_for_status(str(job["job_id"]), "cancelled")

                self.assertEqual(cancelled["status"], "cancelled")
                self.assertFalse(output_path.exists())
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
