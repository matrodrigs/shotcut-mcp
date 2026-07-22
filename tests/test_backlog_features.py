from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import platform, render_jobs
from shotcut_mcp import render as render_module
from shotcut_mcp.errors import ToolError
from shotcut_mcp.path_policy import project_network_resources
from shotcut_mcp.project import (
    ProjectDocument,
    create_project,
    diagnose_missing_media,
    edit_project,
)
from shotcut_mcp.tools import render_contact_sheet_tool


class BacklogProjectFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        validation = patch(
            "shotcut_mcp.project.validate_project_file", return_value={"valid": True}
        )
        validation.start()
        self.addCleanup(validation.stop)

    @staticmethod
    def _media_patch() -> object:
        return patch(
            "shotcut_mcp.project_document.probe_media_raw",
            return_value={"format": {"duration": "10"}, "streams": []},
        )

    def test_new_projects_use_canonical_processing_mode_and_semantic_hdr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "color.mlt"
            created = create_project({"project_path": str(path)})
            self.assertEqual(
                created["project"]["color_workflow"]["processing_mode"],
                "Native8Cpu",
            )
            changed = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": created["revision"],
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
                changed["project"]["color_workflow"],
                {
                    "processing_mode": "Native10Cpu",
                    "color_transfer": "arib-std-b67",
                    "colorspace": "2020",
                    "dynamic_range": "hlg",
                },
            )

    def test_constant_speed_and_positive_speed_map_use_owned_mlt_primitives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            path = root / "speed.mlt"
            created = create_project(
                {"project_path": str(path), "clips": [{"path": str(media)}]}
            )
            sped = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "set_clip_speed",
                            "track": "V1",
                            "item_index": 0,
                            "speed": 2,
                            "pitch_compensation": True,
                        }
                    ],
                }
            )
            self.assertEqual(
                sped["project"]["tracks"][0]["items"][0]["duration_frames"], 150
            )
            producer_id = sped["project"]["tracks"][0]["items"][0]["producer_id"]
            producer = ProjectDocument.load(path).id_map()[producer_id]
            props = {
                item.get("name"): item.text for item in producer.findall("property")
            }
            self.assertEqual(props["mlt_service"], "timewarp")
            self.assertEqual(props["warp_speed"], "2")

            # A fresh source proves timeremap without speculatively merging timewarp.
            second_path = root / "ramp.mlt"
            second = create_project(
                {"project_path": str(second_path), "clips": [{"path": str(media)}]}
            )
            ramped = edit_project(
                {
                    "project_path": str(second_path),
                    "expected_revision": second["revision"],
                    "operations": [
                        {
                            "op": "set_clip_speed_map",
                            "track": "V1",
                            "item_index": 0,
                            "keyframes": [
                                {"frame": 0, "speed": 1},
                                {"frame": 100, "speed": 2},
                            ],
                        }
                    ],
                }
            )
            self.assertEqual(ramped["operation_results"][0]["duration_frames"], 175)
            document = ProjectDocument.load(second_path)
            producer_id = ramped["project"]["tracks"][0]["items"][0]["producer_id"]
            chain = document.id_map()[producer_id]
            self.assertEqual(chain.tag, "chain")
            self.assertEqual(
                next(
                    prop.text
                    for prop in chain.find("link").findall("property")
                    if prop.get("name") == "speed_map"
                ),
                "0=1;100=2",
            )

    def test_slide_and_non_ripple_trim_preserve_total_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            path = root / "timeline.mlt"
            created = create_project({"project_path": str(path)})
            clips = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_clip",
                            "track": "V1",
                            "path": str(media),
                            "in_frame": 50,
                            "out_frame": 99,
                        }
                        for _ in range(3)
                    ],
                }
            )
            slid = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": clips["revision"],
                    "operations": [
                        {
                            "op": "slide_item",
                            "track": "V1",
                            "item_index": 1,
                            "delta": 10,
                        }
                    ],
                }
            )
            self.assertEqual(slid["project"]["duration_frames"], 150)
            items = slid["project"]["tracks"][0]["items"]
            self.assertEqual([item["duration_frames"] for item in items], [60, 50, 40])

            trimmed = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": slid["revision"],
                    "operations": [
                        {
                            "op": "trim_item",
                            "track": "V1",
                            "item_index": 2,
                            "edge": "end",
                            "delta": -10,
                            "ripple": False,
                        }
                    ],
                }
            )
            self.assertEqual(trimmed["project"]["duration_frames"], 150)
            self.assertEqual(
                trimmed["project"]["tracks"][0]["items"][-1]["type"], "gap"
            )

    def test_allowed_roots_cover_media_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            media = root / "outside.mp4"
            media.write_bytes(b"media")
            with (
                patch.dict(os.environ, {"SHOTCUT_MCP_ALLOWED_ROOTS": str(allowed)}),
                self.assertRaisesRegex(ToolError, "allowed roots"),
            ):
                create_project(
                    {
                        "project_path": str(allowed / "project.mlt"),
                        "clips": [{"path": str(media)}],
                    }
                )

    def test_resource_policy_sees_timewarp_and_filter_resource_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remote.mlt"
            path.write_text(
                "<mlt><producer><property name='warp_resource'>https://x/a.mp4</property>"
                "<property name='src'>smb://host/b.png</property></producer></mlt>",
                encoding="utf-8",
            )
            self.assertEqual(
                project_network_resources(path),
                ["https://x/a.mp4", "smb://host/b.png"],
            )

    def test_missing_media_uses_shotcut_hash_before_basename(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            original = root / "original.mp4"
            original.write_bytes(b"same-content")
            project_path = root / "missing.mlt"
            create_project(
                {
                    "project_path": str(project_path),
                    "clips": [{"path": str(original)}],
                }
            )
            original.unlink()
            search = root / "search"
            search.mkdir()
            replacement = search / "renamed.mov"
            replacement.write_bytes(b"same-content")
            result = diagnose_missing_media(
                {
                    "project_path": str(project_path),
                    "search_roots": [str(search)],
                }
            )
            candidate = result["resources"][0]["candidates"][0]
            self.assertEqual(candidate["match"], "shotcut_hash")
            self.assertTrue(candidate["verified"])

    def test_contact_sheet_sampling_crosses_the_project_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            project_path = root / "contact-sheet.mlt"
            create_project(
                {
                    "project_path": str(project_path),
                    "clips": [{"path": str(media)}],
                }
            )
            duration = ProjectDocument.load(project_path).snapshot()["duration_frames"]
            with patch(
                "shotcut_mcp.project._render_contact_sheet",
                return_value={"created": True, "path": str(root / "sheet.png")},
            ) as render:
                result = render_contact_sheet_tool(
                    {
                        "project_path": str(project_path),
                        "output_path": str(root / "sheet.png"),
                        "sample_count": 4,
                    }
                )
            self.assertTrue(result["created"])
            self.assertEqual(
                render.call_args.args[2],
                [round(index * (duration - 1) / 3) for index in range(4)],
            )


class BacklogPlatformFeatureTests(unittest.TestCase):
    def test_batch_preview_reports_per_item_failures(self) -> None:
        project = Path("project.mlt")
        with patch(
            "shotcut_mcp.platform.render_preview",
            side_effect=[
                {"created": True, "path": "one.png", "frame": 1, "size_bytes": 3},
                ToolError("failed"),
            ],
        ):
            result = platform.render_preview_batch(
                project, [(1, Path("one.png")), (2, Path("two.png"))]
            )
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["results"][1]["error"], "failed")

    def test_hardware_encoder_detection_distinguishes_advertised_from_working(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ffmpeg = Path(directory) / "ffmpeg"
            ffmpeg.write_bytes(b"binary")

            def run(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if "-encoders" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        " V..... h264_nvenc NVIDIA\n V..... h264_qsv Intel\n",
                        "",
                    )
                output = Path(command[-1])
                if "h264_nvenc" in command:
                    output.write_bytes(b"encoded")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 1, "", "device unavailable")

            platform._ENCODER_CACHE.clear()
            with (
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=SimpleNamespace(ffmpeg=ffmpeg),
                ),
                patch("shotcut_mcp.platform.run_capture", side_effect=run),
            ):
                result = platform.detect_hardware_encoders(refresh=True)
            states = {item["encoder"]: item["state"] for item in result["candidates"]}
            self.assertEqual(states["h264_nvenc"], "smoke_tested")
            self.assertEqual(states["h264_qsv"], "advertised")

    def test_process_capture_enforces_output_budget(self) -> None:
        with self.assertRaisesRegex(ToolError, "output limit"):
            platform.run_capture(
                [os.sys.executable, "-c", "print('x' * 10000)"],
                max_output_bytes=1024,
            )


class RenderMonitoringFeatureTests(unittest.TestCase):
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
