from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import platform
from shotcut_mcp.errors import ToolError


class PlatformFeatureTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
