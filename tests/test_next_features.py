from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import render as render_module
from shotcut_mcp import render_worker, server
from shotcut_mcp.errors import ToolError
from shotcut_mcp.media import analyze_media_quality
from shotcut_mcp.project import create_project, edit_project, export_marker_chapters
from shotcut_mcp.protocol import report_progress
from shotcut_mcp.storage import OutputTransaction


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


class ProgressProtocolTests(unittest.TestCase):
    def test_concurrent_requests_keep_progress_tokens_isolated(self) -> None:
        barrier = threading.Barrier(2)
        notifications: list[tuple[object, str | None]] = []

        def handler(arguments: dict[str, object]) -> dict[str, object]:
            barrier.wait(timeout=2)
            message = str(arguments["path"])
            report_progress(1, 1, message)
            return {"path": message}

        def call(token: str) -> dict[str, object] | None:
            return server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": token,
                    "method": "tools/call",
                    "params": {
                        "_meta": {"progressToken": token},
                        "name": "probe_media",
                        "arguments": {"path": token},
                    },
                },
                progress_callback=lambda progress_token, _progress, _total, message: (
                    notifications.append((progress_token, message))
                ),
            )

        with (
            patch.dict(server.HANDLERS, {"probe_media": handler}),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            responses = list(executor.map(call, ("request-a", "request-b")))
        self.assertTrue(
            all(response and "error" not in response for response in responses)
        )
        self.assertCountEqual(
            notifications,
            [("request-a", "request-a"), ("request-b", "request-b")],
        )

    def test_stdio_progress_is_token_scoped_and_revision_shaped(self) -> None:
        def handler(_arguments: dict[str, object]) -> dict[str, object]:
            report_progress(0, 2, "Starting")
            report_progress(0, 2, "Duplicate")
            report_progress(2, 2, "Complete")
            return {"ready": True}

        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "_meta": {"progressToken": "qc-2"},
                    "name": "shotcut_status",
                    "arguments": {},
                },
            },
        ]
        input_stream = io.BytesIO(
            ("\n".join(json.dumps(item) for item in messages) + "\n").encode()
        )
        output_stream = io.BytesIO()
        with patch.dict(server.HANDLERS, {"shotcut_status": handler}):
            server.serve(input_stream, output_stream)
        payloads = [
            json.loads(line) for line in output_stream.getvalue().decode().splitlines()
        ]
        progress = [
            item for item in payloads if item.get("method") == "notifications/progress"
        ]
        self.assertEqual([item["params"]["progress"] for item in progress], [0.0, 2.0])
        self.assertTrue(
            all(item["params"]["progressToken"] == "qc-2" for item in progress)
        )
        self.assertTrue(all("message" not in item["params"] for item in progress))
        self.assertEqual(payloads[-1]["id"], 2)

    def test_invalid_progress_token_is_rejected(self) -> None:
        for token in (True, None):
            with self.subTest(token=token):
                response = server.handle_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "_meta": {"progressToken": token},
                            "name": "shotcut_status",
                            "arguments": {},
                        },
                    }
                )
                assert response is not None
                self.assertEqual(response["error"]["code"], -32602)

    def test_current_protocol_progress_includes_the_bounded_message(self) -> None:
        notifications: list[tuple[object, float, float | None, str | None]] = []

        def handler(_arguments: dict[str, object]) -> dict[str, object]:
            report_progress(1, 1, "Complete")
            return {"ready": True}

        with patch.dict(server.HANDLERS, {"shotcut_status": handler}):
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "_meta": {"progressToken": 7},
                        "name": "shotcut_status",
                        "arguments": {},
                    },
                },
                progress_callback=lambda token, progress, total, message: (
                    notifications.append((token, progress, total, message))
                ),
            )
        assert response is not None
        self.assertNotIn("error", response)
        self.assertEqual(notifications, [(7, 1.0, 1.0, "Complete")])


if __name__ == "__main__":
    unittest.main()
