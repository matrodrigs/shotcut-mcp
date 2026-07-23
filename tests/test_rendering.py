from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import render as render_module
from shotcut_mcp.errors import ToolError
from shotcut_mcp.platform import render_preview
from shotcut_mcp.render import cancel_render, render_status, start_render


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


class RenderLifecycleTests(unittest.TestCase):
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

                worker.wait(timeout=15)
                status = render_status(str(job["job_id"]))
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
                worker.wait(timeout=15)
                cancelled = render_status(str(job["job_id"]))

                self.assertEqual(cancelled["status"], "cancelled")
                self.assertFalse(output_path.exists())
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
