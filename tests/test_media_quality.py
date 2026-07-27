from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp.media import analyze_media_quality


class MediaQualityTests(unittest.TestCase):
    def test_quality_analyzers_return_normalized_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "source.mp4"
            media_path.write_bytes(b"media")
            ffmpeg = Path(directory) / "ffmpeg"
            ffmpeg.write_bytes(b"binary")

            def analyze(command: list[str], **_kwargs: object) -> SimpleNamespace:
                if "-filters" in command:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(
                            " .. ebur128 A->N\n .. silencedetect A->A\n"
                            " .S blackdetect V->V\n .. freezedetect V->V\n"
                            " .. idet V->V\n"
                        ),
                        stderr="",
                    )
                filter_text = (
                    command[command.index("-af") + 1]
                    if "-af" in command
                    else command[command.index("-vf") + 1]
                )
                if "silencedetect" in filter_text:
                    text = "silence_start: 1\nsilence_end: 3 | silence_duration: 2\n"
                elif "blackdetect" in filter_text:
                    text = "black_start:2 black_end:4 black_duration:2\n"
                elif "freezedetect" in filter_text:
                    text = "freeze_start: 5\nfreeze_duration: 2\nfreeze_end: 7\n"
                elif filter_text == "idet":
                    text = (
                        "Repeated Fields: Neither: 10 Top: 1 Bottom: 2\n"
                        "Single frame detection: TFF: 3 BFF: 4 Progressive: 5 Undetermined: 6\n"
                        "Multi frame detection: TFF: 7 BFF: 8 Progressive: 9 Undetermined: 10\n"
                    )
                else:
                    text = (
                        "Summary:\nIntegrated loudness:\n I: -23.1 LUFS\n"
                        "Loudness range:\n LRA: 4.2 LU\n LRA low: -25.0 LUFS\n"
                        " LRA high: -20.8 LUFS\nTrue peak:\n Peak: -1.2 dBFS\n"
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr=text)

            probe = {
                "format": {"duration": "12"},
                "streams": [
                    {"index": 0, "codec_type": "video"},
                    {"index": 1, "codec_type": "audio"},
                ],
            }
            with (
                patch("shotcut_mcp.media.probe_media_raw", return_value=probe),
                patch(
                    "shotcut_mcp.media.discover_executables",
                    return_value=SimpleNamespace(ffmpeg=ffmpeg),
                ),
                patch("shotcut_mcp.media.require_executable", return_value=ffmpeg),
                patch("shotcut_mcp.media.run_capture", side_effect=analyze) as run,
            ):
                result = analyze_media_quality(media_path, {})

            self.assertEqual(
                result["analyzers"]["silence"]["streams"][0]["intervals"][0],
                {"start_seconds": 1.0, "end_seconds": 3.0, "duration_seconds": 2.0},
            )
            self.assertEqual(
                result["analyzers"]["black"]["streams"][0]["intervals"][0][
                    "duration_seconds"
                ],
                2.0,
            )
            self.assertEqual(
                result["analyzers"]["interlace"]["streams"][0]["multi_frame_detection"][
                    "progressive"
                ],
                9,
            )
            self.assertEqual(
                result["analyzers"]["loudness"]["streams"][0]["integrated_lufs"],
                -23.1,
            )
            self.assertEqual(run.call_count, 6)
            self.assertTrue(
                all(isinstance(call.args[0], list) for call in run.call_args_list)
            )


if __name__ == "__main__":
    unittest.main()
