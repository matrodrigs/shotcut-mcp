from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp.errors import ToolError
from shotcut_mcp.media import analyze_media_quality, summarize_media


class MediaQualityTests(unittest.TestCase):
    def test_failed_and_skipped_analyzers_preserve_successful_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"media")
            ffmpeg = Path(directory) / "ffmpeg"
            ffmpeg.write_bytes(b"binary")

            def analyze(command: list[str], **_kwargs: object) -> SimpleNamespace:
                if "-filters" in command:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=" .. silencedetect A->A\n .. ebur128 A->N\n .. blackdetect V->V\n",
                        stderr="",
                    )
                if "ebur128" in command[command.index("-af") + 1]:
                    return SimpleNamespace(
                        returncode=1, stdout="", stderr="analysis failed"
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch(
                    "shotcut_mcp.media.probe_media_raw",
                    return_value={"streams": [{"index": 1, "codec_type": "audio"}]},
                ),
                patch(
                    "shotcut_mcp.media.discover_executables",
                    return_value=SimpleNamespace(ffmpeg=ffmpeg),
                ),
                patch("shotcut_mcp.media.run_capture", side_effect=analyze),
            ):
                result = analyze_media_quality(
                    source, {"analyzers": ["silence", "loudness", "black", "freeze"]}
                )
            self.assertEqual(
                result["analyzers"],
                {
                    "silence": {
                        "status": "ok",
                        "filter": "silencedetect",
                        "streams": [
                            {
                                "status": "ok",
                                "stream_index": 1,
                                "intervals": [],
                                "intervals_truncated": False,
                            }
                        ],
                    },
                    "loudness": {
                        "status": "failed",
                        "filter": "ebur128",
                        "streams": [
                            {
                                "status": "failed",
                                "stream_index": 1,
                                "error": "analysis failed",
                            }
                        ],
                    },
                    "black": {
                        "status": "not_applicable",
                        "filter": "blackdetect",
                        "streams": [],
                        "reason": "The media has no video stream.",
                    },
                    "freeze": {
                        "status": "unavailable",
                        "filter": "freezedetect",
                        "streams": [],
                        "reason": "FFmpeg filter freezedetect is not installed.",
                    },
                },
            )
            self.assertEqual(source.read_bytes(), b"media")

    def test_success_without_ffmpeg_metrics_keeps_existing_result_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"media")
            ffmpeg = Path(directory) / "ffmpeg"
            ffmpeg.write_bytes(b"binary")

            def analyze(command: list[str], **_kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    returncode=0,
                    stderr="",
                    stdout=" .. idet V->V\n .. ebur128 A->N\n"
                    if "-filters" in command
                    else "",
                )

            with (
                patch(
                    "shotcut_mcp.media.probe_media_raw",
                    return_value={
                        "streams": [
                            {"index": 0, "codec_type": "video"},
                            {"index": 1, "codec_type": "audio"},
                        ]
                    },
                ),
                patch(
                    "shotcut_mcp.media.discover_executables",
                    return_value=SimpleNamespace(ffmpeg=ffmpeg),
                ),
                patch("shotcut_mcp.media.run_capture", side_effect=analyze),
            ):
                result = analyze_media_quality(
                    source, {"analyzers": ["interlace", "loudness"]}
                )
            self.assertEqual(
                result["analyzers"]["interlace"]["streams"],
                [{"stream_index": 0, "status": "ok"}],
            )
            self.assertEqual(
                result["analyzers"]["loudness"]["streams"],
                [
                    {
                        "stream_index": 1,
                        "status": "ok",
                        "integrated_lufs": None,
                        "loudness_range_lu": None,
                        "lra_low_lufs": None,
                        "lra_high_lufs": None,
                        "true_peak_dbfs": None,
                    }
                ],
            )

    def test_malformed_probe_data_reports_a_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ffprobe = root / "ffprobe"
            ffprobe.write_bytes(b"executable")
            for index, payload in enumerate(
                (
                    {"streams": "invalid"},
                    {"format": []},
                    {"streams": ["invalid"]},
                    {"streams": [{"codec_type": "video", "width": "wide"}]},
                    {"streams": [{"codec_type": "audio", "channels": True}]},
                )
            ):
                with self.subTest(payload=payload):
                    source = root / f"source-{index}.mp4"
                    source.write_bytes(b"media")
                    with (
                        patch(
                            "shotcut_mcp.media.discover_executables",
                            return_value=SimpleNamespace(ffprobe=ffprobe),
                        ),
                        patch(
                            "shotcut_mcp.media.run_capture",
                            return_value=SimpleNamespace(
                                returncode=0, stdout=json.dumps(payload), stderr=""
                            ),
                        ),
                        self.assertRaises(ToolError) as caught,
                    ):
                        summarize_media(source)
                    self.assertEqual(caught.exception.code, "media_probe_failed")
                    self.assertEqual(source.read_bytes(), b"media")

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
