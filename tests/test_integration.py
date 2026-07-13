from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from shotcut_mcp.platform import discover_executables, render_preview
from shotcut_mcp.project import create_project, edit_project
from shotcut_mcp.render import render_status, start_render

PLUGIN_ROOT = Path(__file__).parents[1]


@unittest.skipUnless(
    os.environ.get("SHOTCUT_MCP_INTEGRATION") == "1", "real Shotcut integration"
)
class RealShotcutIntegrationTests(unittest.TestCase):
    def test_create_edit_preview_validate_and_render(self) -> None:
        executables = discover_executables()
        self.assertIsNotNone(executables.ffmpeg)
        self.assertIsNotNone(executables.melt)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            subprocess.run(
                [
                    str(executables.ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=0x3366cc:s=320x240:d=2:r=30",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    "-shortest",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(media),
                ],
                check=True,
                timeout=30,
            )
            project = create_project(
                {
                    "project_path": str(root / "timeline.mlt"),
                    "width": 320,
                    "height": 240,
                    "fps_num": 30,
                    "clips": [
                        {"path": str(media), "in_frame": 0, "out_frame": 29},
                        {"path": str(media), "in_frame": 30, "out_frame": 59},
                    ],
                    "validate": True,
                }
            )
            edited = edit_project(
                {
                    "project_path": project["path"],
                    "expected_revision": project["revision"],
                    "validate": True,
                    "operations": [
                        {
                            "op": "add_transition",
                            "track": "V1",
                            "left_item_index": 0,
                            "duration_frames": 10,
                        },
                        {"op": "add_track", "kind": "video", "name": "Titles"},
                        {
                            "op": "add_generator",
                            "track": "Titles",
                            "generator": "text",
                            "text": "Shotcut MCP",
                            "duration_frames": 30,
                            "position_frame": 0,
                            "mode": "overwrite",
                        },
                    ],
                }
            )
            preview = render_preview(
                Path(edited["path"]), root / "preview.png", 10, False
            )
            self.assertGreater(preview["size_bytes"], 100)
            job = start_render(
                {
                    "project_path": edited["path"],
                    "output_path": str(root / "export.mp4"),
                    "preset": "h264-web",
                }
            )
            deadline = time.time() + 60
            while time.time() < deadline:
                result = render_status(job["job_id"])
                if result["status"] != "running":
                    break
                time.sleep(0.2)
            self.assertEqual(result["status"], "completed", result.get("log_tail"))
            self.assertTrue(result["output_exists"])
            self.assertGreater(result["output_size_bytes"], 1000)
