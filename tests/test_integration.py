from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from shotcut_mcp.platform import (
    analyze_media_quality,
    discover_executables,
    render_contact_sheet,
    render_preview,
    summarize_media,
)
from shotcut_mcp.project import create_project, edit_project
from shotcut_mcp.render import cancel_render, render_status, start_render

PLUGIN_ROOT = Path(__file__).parents[1]


@unittest.skipUnless(
    os.environ.get("SHOTCUT_MCP_INTEGRATION") == "1", "real Shotcut integration"
)
class RealShotcutIntegrationTests(unittest.TestCase):
    @staticmethod
    def _create_media(ffmpeg: Path, media: Path, duration: int = 2) -> None:
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x3366cc:s=320x240:d={duration}:r=30",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={duration}",
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

    @staticmethod
    def _create_image(ffmpeg: Path, image: Path, color: str) -> None:
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=64x64",
                "-frames:v",
                "1",
                str(image),
            ],
            check=True,
            timeout=30,
        )

    @staticmethod
    def _preview_pixel(ffmpeg: Path, preview: Path) -> tuple[int, int, int]:
        raw = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(preview),
                "-vf",
                "scale=1:1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        if len(raw) < 3:
            raise AssertionError("Preview did not contain an RGB pixel.")
        return raw[0], raw[1], raw[2]

    def test_create_edit_preview_validate_and_render(self) -> None:
        executables = discover_executables()
        self.assertIsNotNone(executables.ffmpeg)
        self.assertIsNotNone(executables.melt)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            self._create_media(executables.ffmpeg, media)
            quality = analyze_media_quality(media, {"analyzers": ["black", "loudness"]})
            self.assertEqual(quality["analyzers"]["black"]["status"], "ok")
            self.assertEqual(quality["analyzers"]["loudness"]["status"], "ok")
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
                        {
                            "op": "add_marker",
                            "start_frame": 0,
                            "end_frame": 20,
                            "text": "Opening",
                            "color": "#00A0FF",
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
                    "marker_id": edited["operation_results"][-1]["marker_id"],
                }
            )
            self.assertEqual((job["in_frame"], job["out_frame"]), (0, 19))
            deadline = time.time() + 60
            while time.time() < deadline:
                result = render_status(job["job_id"])
                if result["status"] != "running":
                    break
                time.sleep(0.2)
            self.assertEqual(result["status"], "completed", result.get("log_tail"))
            self.assertTrue(result["output_exists"])
            self.assertGreater(result["output_size_bytes"], 1000)

    def test_timewarp_timeremap_and_contact_sheet_validate_with_real_mlt(self) -> None:
        executables = discover_executables()
        assert executables.ffmpeg is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            self._create_media(executables.ffmpeg, media, duration=4)

            constant_path = root / "constant.mlt"
            constant = create_project(
                {
                    "project_path": str(constant_path),
                    "width": 320,
                    "height": 240,
                    "fps_num": 30,
                    "clips": [{"path": str(media)}],
                }
            )
            constant = edit_project(
                {
                    "project_path": str(constant_path),
                    "expected_revision": constant["revision"],
                    "operations": [
                        {
                            "op": "set_clip_speed",
                            "track": "V1",
                            "item_index": 0,
                            "speed": 2,
                        }
                    ],
                }
            )
            preview = render_preview(constant_path, root / "constant.png", 10, False)
            self.assertGreater(preview["size_bytes"], 100)
            sheet = render_contact_sheet(
                constant_path,
                root / "sheet.png",
                [0, 10, 20, 30],
                columns=2,
                cell_width=160,
                overwrite=False,
            )
            self.assertGreater(sheet["size_bytes"], 100)

            ramp_path = root / "ramp.mlt"
            ramp = create_project(
                {
                    "project_path": str(ramp_path),
                    "width": 320,
                    "height": 240,
                    "fps_num": 30,
                    "clips": [{"path": str(media)}],
                }
            )
            ramp = edit_project(
                {
                    "project_path": str(ramp_path),
                    "expected_revision": ramp["revision"],
                    "operations": [
                        {
                            "op": "set_clip_speed_map",
                            "track": "V1",
                            "item_index": 0,
                            "keyframes": [
                                {"frame": 0, "speed": 1},
                                {"frame": 30, "speed": 2},
                            ],
                        }
                    ],
                }
            )
            self.assertTrue(ramp["validation"]["valid"])
            preview = render_preview(ramp_path, root / "ramp.png", 10, False)
            self.assertGreater(preview["size_bytes"], 100)

    def test_still_images_render_after_split_and_replacement(self) -> None:
        executables = discover_executables()
        assert executables.ffmpeg is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            red = root / "red.png"
            green = root / "green.png"
            self._create_image(executables.ffmpeg, red, "red")
            self._create_image(executables.ffmpeg, green, "green")
            project_path = root / "images.mlt"
            created = create_project(
                {
                    "project_path": str(project_path),
                    "width": 64,
                    "height": 64,
                    "fps_num": 30,
                    "clips": [{"path": str(red), "image_duration_seconds": 1.5}],
                }
            )
            split = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "split_item",
                            "track": "V1",
                            "item_index": 0,
                            "offset_frame": 15,
                        }
                    ],
                }
            )
            first_preview = root / "first.png"
            render_preview(project_path, first_preview, 10, False)
            first_pixel = self._preview_pixel(executables.ffmpeg, first_preview)
            self.assertGreater(first_pixel[0], 120)
            self.assertLess(max(first_pixel[1:]), 30)

            replaced = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": split["revision"],
                    "operations": [
                        {
                            "op": "replace_item_media",
                            "track": "V1",
                            "item_index": 1,
                            "path": str(green),
                        }
                    ],
                }
            )
            second = replaced["project"]["tracks"][0]["items"][1]
            self.assertEqual((second["in_frame"], second["out_frame"]), (15, 44))
            second_preview = root / "second.png"
            render_preview(project_path, second_preview, 30, False)
            second_pixel = self._preview_pixel(executables.ffmpeg, second_preview)
            self.assertGreater(second_pixel[1], 60)
            self.assertLess(max(second_pixel[0], second_pixel[2]), 30)

    def test_structured_opacity_preserves_color_during_composition(self) -> None:
        executables = discover_executables()
        assert executables.ffmpeg is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "opacity.mlt"
            created = create_project(
                {
                    "project_path": str(project_path),
                    "width": 64,
                    "height": 64,
                    "fps_num": 30,
                }
            )
            tracked = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {"op": "add_track", "kind": "video", "name": "Overlay"}
                    ],
                }
            )
            edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": tracked["revision"],
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 25,
                            "color": "#0000ff",
                        },
                        {
                            "op": "add_generator",
                            "track": "Overlay",
                            "generator": "color",
                            "duration_frames": 25,
                            "color": "#ff0000",
                        },
                        {
                            "op": "split_item",
                            "track": "Overlay",
                            "item_index": 0,
                            "offset_frame": 10,
                        },
                        {
                            "op": "set_clip_opacity",
                            "track": "Overlay",
                            "item_index": 1,
                            "opacity_keyframes": [
                                {"frame": 0, "opacity": 0},
                                {"frame": 14, "opacity": 1},
                            ],
                        },
                    ],
                }
            )
            preview = root / "midpoint.png"
            render_preview(project_path, preview, 17, False)
            red, green, blue = self._preview_pixel(executables.ffmpeg, preview)
            self.assertGreater(red, 100)
            self.assertLess(green, 15)
            self.assertLessEqual(abs(red - blue), 15)

    def test_hlg_workflow_and_named_10bit_export_preset(self) -> None:
        executables = discover_executables()
        assert executables.ffmpeg is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            self._create_media(executables.ffmpeg, media, duration=1)
            project_path = root / "hlg.mlt"
            project = create_project(
                {
                    "project_path": str(project_path),
                    "width": 320,
                    "height": 240,
                    "fps_num": 30,
                    "clips": [{"path": str(media), "out_frame": 2}],
                }
            )
            project = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": project["revision"],
                    "operations": [
                        {
                            "op": "set_color_workflow",
                            "workflow": "hlg",
                            "processing_mode": "Native10Cpu",
                        }
                    ],
                }
            )
            self.assertEqual(
                project["project"]["color_workflow"]["color_transfer"],
                "arib-std-b67",
            )
            output = root / "hlg.mp4"
            job = start_render(
                {
                    "project_path": str(project_path),
                    "output_path": str(output),
                    "preset": "hdr-hlg-hevc",
                    "consumer_properties": {"preset": "ultrafast"},
                }
            )
            deadline = time.time() + 120
            try:
                while time.time() < deadline:
                    result = render_status(job["job_id"])
                    if result["status"] != "running":
                        break
                    time.sleep(0.2)
            finally:
                latest = render_status(job["job_id"])
                if latest["status"] == "running":
                    cancel_render(job["job_id"])
            self.assertEqual(result["status"], "completed", result.get("log_tail"))
            summary = summarize_media(output)
            video = next(
                stream for stream in summary["streams"] if stream["type"] == "video"
            )
            self.assertEqual(video["color_transfer"], "arib-std-b67")
            self.assertGreaterEqual(video["pixel_bit_depth"], 10)
