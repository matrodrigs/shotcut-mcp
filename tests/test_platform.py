from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp import platform
from shotcut_mcp.errors import ToolError


class MeltCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        platform._MELT_READY_CACHE.clear()
        platform._SERVICE_CACHE.clear()

    def test_repository_environment_is_part_of_readiness_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            melt = Path(directory) / "melt"
            melt.write_bytes(b"executable")
            completed = subprocess.CompletedProcess([], 0, "consumers", "")
            with patch("shotcut_mcp.platform.run_capture", return_value=completed) as run:
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
            ):
                with self.assertRaisesRegex(ToolError, "repository failure"):
                    platform.list_services("filter")

    def test_doctor_checks_rnnoise_independently_from_repository_preflight(self) -> None:
        executables = platform.Executables(
            Path("shotcut"), Path("melt"), Path("ffprobe"), Path("ffmpeg")
        )
        unavailable = {"available": False, "metadata": "# No metadata"}
        with (
            patch("shotcut_mcp.platform.discover_executables", return_value=executables),
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


class PathPolicyTests(unittest.TestCase):
    def test_configured_allowed_roots_block_paths_outside_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            allowed = Path(directory) / "allowed"
            outside = Path(directory) / "outside.mlt"
            allowed.mkdir()
            with patch.dict(
                os.environ, {"SHOTCUT_MCP_ALLOWED_ROOTS": str(allowed)}, clear=False
            ):
                with self.assertRaisesRegex(ToolError, "allowed roots"):
                    platform.expand_path(str(outside))


if __name__ == "__main__":
    unittest.main()
