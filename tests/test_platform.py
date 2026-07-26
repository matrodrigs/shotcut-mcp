from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp import platform
from shotcut_mcp.errors import RequestCancelled, ToolError
from shotcut_mcp.protocol import request_cancellation


class MeltCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        platform._MELT_READY_CACHE.clear()
        platform._SERVICE_CACHE.clear()

    def test_repository_environment_is_part_of_readiness_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            melt = Path(directory) / "melt"
            melt.write_bytes(b"executable")
            completed = subprocess.CompletedProcess([], 0, "consumers", "")
            with patch(
                "shotcut_mcp.platform.run_capture", return_value=completed
            ) as run:
                with patch.dict(os.environ, {"MLT_REPOSITORY_DENY": "first"}):
                    platform.ensure_melt_ready(melt)
                with patch.dict(os.environ, {"MLT_REPOSITORY_DENY": "second"}):
                    platform.ensure_melt_ready(melt)

            self.assertEqual(run.call_count, 2)

    def test_failed_service_query_is_not_cached_as_an_empty_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            melt = Path(directory) / "melt"
            melt.write_bytes(b"executable")
            failed = subprocess.CompletedProcess([], 2, "", "repository failure")
            with (
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=platform.Executables(None, melt, None, None),
                ),
                patch("shotcut_mcp.platform.ensure_melt_ready"),
                patch("shotcut_mcp.platform.run_capture", return_value=failed),
                self.assertRaisesRegex(ToolError, "repository failure"),
            ):
                platform.list_services("filter")

    def test_doctor_checks_rnnoise_independently_from_repository_preflight(
        self,
    ) -> None:
        executables = platform.Executables(
            Path("shotcut"), Path("melt"), Path("ffprobe"), Path("ffmpeg")
        )
        unavailable = {"available": False, "metadata": "# No metadata"}
        with (
            patch(
                "shotcut_mcp.platform.discover_executables", return_value=executables
            ),
            patch("shotcut_mcp.platform.ensure_melt_ready"),
            patch(
                "shotcut_mcp.platform.version_line",
                side_effect=["Shotcut 26.6.25", "melt 7.40.0"],
            ),
            patch("shotcut_mcp.platform.describe_service", return_value=unavailable),
        ):
            result = platform.compatibility_doctor()

        self.assertTrue(result["checks"]["repository"]["passed"])
        self.assertFalse(result["checks"]["rnnoise"]["passed"])
        self.assertFalse(result["compatible"])

    def test_doctor_reports_quality_analyzers_without_changing_compatibility(
        self,
    ) -> None:
        executables = platform.Executables(
            Path("shotcut"), Path("melt"), Path("ffprobe"), Path("ffmpeg")
        )
        analyzers = {
            "silence": {
                "filter": "silencedetect",
                "stream_type": "audio",
                "available": True,
            },
            "freeze": {
                "filter": "freezedetect",
                "stream_type": "video",
                "available": False,
            },
        }
        with (
            patch(
                "shotcut_mcp.platform.discover_executables", return_value=executables
            ),
            patch("shotcut_mcp.platform.ensure_melt_ready"),
            patch(
                "shotcut_mcp.platform.version_line",
                side_effect=["Shotcut 26.6.25", "melt 7.40.0"],
            ),
            patch(
                "shotcut_mcp.platform.describe_service",
                return_value={"available": True, "metadata": "available"},
            ),
            patch(
                "shotcut_mcp.platform.quality_analyzer_capabilities",
                return_value=analyzers,
                create=True,
            ),
        ):
            result = platform.compatibility_doctor()

        self.assertTrue(result["compatible"])
        self.assertEqual(result["quality_analyzers"], analyzers)
        self.assertFalse(result["quality_analyzers"]["freeze"]["available"])

    def test_media_contact_sheet_reports_when_no_candidate_has_a_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "audio-only.mp3"
            output = root / "candidates.png"
            media.write_bytes(b"audio")
            failed = subprocess.CompletedProcess([], 1, "", "no video stream")
            with (
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=platform.Executables(None, None, None, Path("ffmpeg")),
                ),
                patch(
                    "shotcut_mcp.platform.require_executable",
                    return_value=Path("ffmpeg"),
                ),
                patch("shotcut_mcp.platform.run_capture", return_value=failed),
                self.assertRaises(ToolError) as caught,
            ):
                platform.render_media_contact_sheet(
                    [("candidate-1", media)], output, overwrite=False
                )

            self.assertEqual(caught.exception.code, "no_visual_frame")
            self.assertEqual(caught.exception.recommended_tool, "probe_media")
            self.assertEqual(caught.exception.details["candidate_count"], 1)
            self.assertEqual(
                caught.exception.details["skipped"][0]["candidate_id"], "candidate-1"
            )
            self.assertFalse(output.exists())

    def test_encoder_query_failure_recommends_compatibility_diagnostics(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "encoder query failed")
        platform._ENCODER_CACHE.clear()
        with tempfile.TemporaryDirectory() as directory:
            ffmpeg = Path(directory) / "ffmpeg"
            ffmpeg.write_bytes(b"executable")
            with (
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=platform.Executables(None, None, None, ffmpeg),
                ),
                patch("shotcut_mcp.platform.require_executable", return_value=ffmpeg),
                patch("shotcut_mcp.platform.run_capture", return_value=failed),
                self.assertRaises(ToolError) as caught,
            ):
                platform.detect_hardware_encoders(refresh=True)

        self.assertEqual(caught.exception.code, "ffmpeg_capability_query_failed")
        self.assertEqual(
            caught.exception.recommended_action, "run_compatibility_diagnostics"
        )
        self.assertEqual(caught.exception.recommended_tool, "shotcut_doctor")
        self.assertEqual(caught.exception.details["query"], "encoders")


class PathPolicyTests(unittest.TestCase):
    def test_configured_allowed_roots_block_paths_outside_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            allowed = Path(directory) / "allowed"
            outside = Path(directory) / "outside.mlt"
            allowed.mkdir()
            with (
                patch.dict(
                    os.environ, {"SHOTCUT_MCP_ALLOWED_ROOTS": str(allowed)}, clear=False
                ),
                self.assertRaisesRegex(ToolError, "allowed roots") as caught,
            ):
                platform.expand_path(str(outside))

            self.assertEqual(caught.exception.code, "path_policy_denied")
            self.assertEqual(caught.exception.recommended_tool, "shotcut_doctor")
            self.assertEqual(caught.exception.details["path"], str(outside.resolve()))

    def test_project_network_resources_are_blocked_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "remote.mlt"
            project_path.write_text(
                '<mlt><producer><property name="resource">'
                "https://example.invalid/video.mp4"
                "</property></producer></mlt>",
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=False),
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=platform.Executables(None, None, None, None),
                ),
            ):
                os.environ.pop("SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES", None)
                with self.assertRaisesRegex(ToolError, "network resources"):
                    platform.validate_project_file(project_path)


class ProcessCancellationTests(unittest.TestCase):
    def test_run_capture_terminates_when_mcp_request_is_cancelled(self) -> None:
        cancellation = threading.Event()
        timer = threading.Timer(0.1, cancellation.set)
        started = time.monotonic()
        timer.start()
        try:
            with (
                request_cancellation(cancellation),
                self.assertRaises(RequestCancelled),
            ):
                platform.run_capture(
                    [os.sys.executable, "-c", "import time; time.sleep(20)"],
                    timeout=30,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
