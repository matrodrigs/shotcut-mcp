from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import platform
from shotcut_mcp.errors import RequestCancelled, ToolError
from shotcut_mcp.protocol import request_cancellation, request_progress


class PlatformFeatureTests(unittest.TestCase):
    def test_previews_preserve_outputs_on_cancellation_and_concurrent_changes(
        self,
    ) -> None:
        for contact_sheet in (False, True):
            for cancel in (False, True):
                with self.subTest(contact_sheet=contact_sheet, cancel=cancel):
                    self._assert_preview_output_protection(contact_sheet, cancel)

    def _assert_preview_output_protection(
        self, contact_sheet: bool, cancel: bool
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project.mlt"
            project.write_text('<mlt><profile width="320" height="240"/></mlt>')
            original = project.read_bytes()
            output = root / "existing.png"
            output.write_bytes(b"existing")
            event = threading.Event()
            intermediates = []
            progress = []

            def render(command, **_kwargs):
                if "-consumer" in command:
                    pattern = next(
                        item.removeprefix("avformat:")
                        for item in command
                        if item.startswith("avformat:")
                    )
                    frame = Path(pattern % 0)
                    frame.write_bytes(b"frame")
                    intermediates.append(frame.parent)
                    if cancel:
                        event.set()
                    else:
                        output.write_bytes(b"concurrent writer")
                else:
                    Path(command[-1]).write_bytes(b"sheet")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=platform.Executables(
                        None, Path("melt"), None, Path("ffmpeg")
                    ),
                ),
                patch("shotcut_mcp.platform.ensure_melt_ready"),
                patch("shotcut_mcp.platform.run_capture", side_effect=render),
                request_cancellation(event),
                request_progress(
                    lambda value, _total, _message: progress.append(value)
                ),
            ):

                def preview():
                    if contact_sheet:
                        return platform.render_contact_sheet(
                            project,
                            output,
                            [0],
                            columns=1,
                            cell_width=320,
                            overwrite=True,
                        )
                    return platform.render_preview_batch(
                        project, [(0, output)], overwrite=True
                    )

                if cancel:
                    with self.assertRaises(RequestCancelled):
                        preview()
                elif contact_sheet:
                    with self.assertRaises(ToolError) as caught:
                        preview()
                    self.assertEqual(caught.exception.code, "output_changed")
                else:
                    self.assertEqual(preview()["created"], 0)
            self.assertEqual(
                output.read_bytes(),
                b"existing" if cancel else b"concurrent writer",
            )
            self.assertEqual(project.read_bytes(), original)
            self.assertTrue(all(not path.exists() for path in intermediates))
            self.assertEqual(list(root.glob(".*.tmp*")), [])
            self.assertEqual(progress, sorted(set(progress)))

    def test_batch_preview_uses_one_renderer_and_preserves_conflicting_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project.mlt"
            project.write_text('<mlt><profile width="320" height="240"/></mlt>')
            first, second, protected = (
                root / "one.png",
                root / "two.png",
                root / "existing.png",
            )
            protected.write_bytes(b"existing")

            def render(command, **_kwargs):
                pattern = next(
                    item.removeprefix("avformat:")
                    for item in command
                    if item.startswith("avformat:")
                )
                if "%06d" in pattern:
                    for index in range(2):
                        Path(pattern % index).write_bytes(f"frame-{index}".encode())
                else:
                    Path(pattern).write_bytes(b"individual frame")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=platform.Executables(None, Path("melt"), None, None),
                ),
                patch("shotcut_mcp.platform.ensure_melt_ready"),
                patch("shotcut_mcp.platform.run_capture", side_effect=render) as run,
            ):
                result = platform.render_preview_batch(
                    project, [(9, first), (2, protected), (1, second)]
                )
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result["created"], 2)
            self.assertEqual(first.read_bytes(), b"frame-0")
            self.assertEqual(second.read_bytes(), b"frame-1")
            self.assertEqual(protected.read_bytes(), b"existing")
            self.assertFalse(result["results"][1]["created"])
            self.assertEqual(list(root.glob(".*.tmp*")), [])

    def test_batch_preview_failure_preserves_existing_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project.mlt"
            project.write_text('<mlt><profile width="320" height="240"/></mlt>')
            output = root / "existing.png"
            output.write_bytes(b"existing")
            with (
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=platform.Executables(None, Path("melt"), None, None),
                ),
                patch("shotcut_mcp.platform.ensure_melt_ready"),
                patch(
                    "shotcut_mcp.platform.run_capture",
                    return_value=subprocess.CompletedProcess(
                        [], 1, "", "render failed"
                    ),
                ),
            ):
                result = platform.render_preview_batch(
                    project, [(1, output)], overwrite=True
                )
            self.assertEqual(result["created"], 0)
            self.assertEqual(output.read_bytes(), b"existing")
            self.assertEqual(list(root.glob(".*.tmp*")), [])

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
