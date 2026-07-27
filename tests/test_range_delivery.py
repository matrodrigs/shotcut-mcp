from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import render as render_module
from shotcut_mcp import render_worker
from shotcut_mcp.errors import ToolError
from shotcut_mcp.project import create_project, edit_project, export_marker_chapters
from shotcut_mcp.storage import OutputTransaction


class RangeRenderAndChapterTests(unittest.TestCase):
    @staticmethod
    def _start(
        arguments: dict[str, object], timing: dict[str, object]
    ) -> dict[str, object]:
        fake_process = SimpleNamespace(pid=4321)
        fake_thread = SimpleNamespace(start=lambda: None)
        job_directory = Path(str(arguments["project_path"])).parent / "render-jobs"
        with (
            patch("shotcut_mcp.render_jobs.JOB_DIR", job_directory),
            patch("shotcut_mcp.render.project_timing", return_value=timing),
            patch(
                "shotcut_mcp.render.discover_executables",
                return_value=SimpleNamespace(melt=Path("melt")),
            ),
            patch("shotcut_mcp.render.require_executable", return_value=Path("melt")),
            patch("shotcut_mcp.render.ensure_melt_ready"),
            patch("shotcut_mcp.render.subprocess.Popen", return_value=fake_process),
            patch("shotcut_mcp.render.threading.Thread", return_value=fake_thread),
        ):
            result = render_module.start_render(arguments)
        render_module.RUNNING_JOBS.pop(str(result["job_id"]), None)
        return result

    def test_range_and_marker_bounds_are_persisted_for_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project.mlt"
            project.write_text("<mlt/>\n", encoding="utf-8")
            timing = {
                "duration_frames": 100,
                "revision": "a" * 64,
                "markers": [
                    {
                        "marker_id": "7",
                        "text": "Scene",
                        "start_frame": 20,
                        "end_frame": 31,
                    }
                ],
            }
            job = self._start(
                {
                    "project_path": str(project),
                    "output_path": str(root / "range.mp4"),
                    "marker_id": "7",
                },
                timing,
            )
            self.assertEqual((job["in_frame"], job["out_frame"]), (20, 30))
            self.assertEqual(job["range_duration_frames"], 11)
            self.assertEqual(job["marker_text"], "Scene")
            output = OutputTransaction.deserialize(job["output_transaction"])
            command = render_worker._command(job, output)
            self.assertEqual(command[2:5], ["in=20", "out=30", "-progress2"])

            with self.assertRaisesRegex(ToolError, "provided together"):
                self._start(
                    {
                        "project_path": str(project),
                        "output_path": str(root / "invalid.mp4"),
                        "in_frame": 10,
                    },
                    timing,
                )

    def test_chapter_export_matches_shotcut_text_format(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True},
            ),
        ):
            root = Path(directory)
            project = root / "chapters.mlt"
            created = create_project({"project_path": str(project)})
            edited = edit_project(
                {
                    "project_path": str(project),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_marker",
                            "start_frame": 30,
                            "text": "Olá",
                            "color": "#00A0FF",
                        },
                        {
                            "op": "add_marker",
                            "start_frame": 60,
                            "end_frame": 90,
                            "text": "Range",
                            "color": "#FF8800",
                        },
                    ],
                }
            )
            output = root / "chapters.txt"
            result = export_marker_chapters(
                {
                    "project_path": str(project),
                    "output_path": str(output),
                    "expected_revision": edited["revision"],
                }
            )
            self.assertEqual(
                output.read_text(encoding="utf-8"), "00:00 Intro\n00:01 Olá\n"
            )
            self.assertEqual(result["marker_count"], 1)
            self.assertEqual(result["chapter_count"], 2)

            range_output = root / "range-chapters.txt"
            range_result = export_marker_chapters(
                {
                    "project_path": str(project),
                    "output_path": str(range_output),
                    "expected_revision": edited["revision"],
                    "include_range_markers": True,
                    "colors": ["#ff8800"],
                }
            )
            self.assertEqual(
                range_output.read_text(encoding="utf-8"),
                "00:00 Intro\n00:02 Range\n",
            )
            self.assertEqual(range_result["marker_count"], 1)


if __name__ == "__main__":
    unittest.main()
