from __future__ import annotations

import hashlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp.errors import ConflictError, RequestCancelled, ToolError
from shotcut_mcp.project import create_project, edit_project
from shotcut_mcp.protocol import request_cancellation
from shotcut_mcp.render import render_review_clip
from shotcut_mcp.storage import managed_preview_path


class ReviewClipTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project.mlt"
        with patch(
            "shotcut_mcp.project.validate_project_file", return_value={"valid": True}
        ):
            created = create_project({"project_path": str(self.project), "fps_num": 30})
            edit_project(
                {
                    "project_path": str(self.project),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 1200,
                        }
                    ],
                }
            )
        self.revision = hashlib.sha256(self.project.read_bytes()).hexdigest()
        self.arguments = {
            "project_path": str(self.project),
            "in_frame": 30,
            "out_frame": 59,
            "expected_revision": self.revision,
        }
        for name, replacement in (
            ("discover_executables", SimpleNamespace(melt=Path("melt"))),
            ("require_executable", Path("melt")),
            ("ensure_melt_ready", None),
        ):
            patcher = patch(f"shotcut_mcp.render.{name}", return_value=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _render(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        target = Path(
            next(part[9:] for part in command if part.startswith("avformat:"))
        )
        target.write_bytes(b"review media")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_review_uses_bounded_managed_output_and_exact_snapshot(self) -> None:
        original = self.project.read_bytes()
        commands: list[list[str]] = []

        def render(
            command: list[str], **options: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            snapshot = next(
                Path(argument) for argument in command if argument.endswith(".mlt")
            )
            self.assertEqual(snapshot.read_bytes(), original)
            self.assertEqual(options["timeout"], 180)
            return self._render(command)

        with patch("shotcut_mcp.render.run_capture", side_effect=render):
            first = render_review_clip(self.arguments)
            second = render_review_clip(self.arguments)
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(first["revision"], self.revision)
        self.assertEqual((first["width"], first["height"]), (1280, 720))
        self.assertEqual(first["duration_frames"], 30)
        self.assertEqual(first["duration_seconds"], 1)
        self.assertEqual(first["mime_type"], "video/mp4")
        self.assertTrue(first["managed_output"])
        self.assertEqual(Path(first["path"]).read_bytes(), b"review media")
        self.assertEqual(self.project.read_bytes(), original)
        self.assertIn("in=30", commands[0])
        self.assertIn("out=59", commands[0])
        self.assertFalse(Path(commands[0][commands[0].index("-profile") + 1]).exists())
        self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])
        self.assertEqual(
            list(Path(first["path"]).parent.glob("*.mp4")), [Path(first["path"])]
        )

    def test_rejects_stale_revision_and_invalid_or_unbounded_ranges(self) -> None:
        with patch("shotcut_mcp.render.run_capture") as capture:
            with self.assertRaises(ConflictError):
                render_review_clip({**self.arguments, "expected_revision": "0" * 64})
            for bounds in ((-1, 0), (True, 30), (10, 9), (0, 1200), (0, 900)):
                with self.subTest(bounds=bounds), self.assertRaises(ToolError):
                    render_review_clip(
                        {
                            **self.arguments,
                            "in_frame": bounds[0],
                            "out_frame": bounds[1],
                        }
                    )
            capture.assert_not_called()

    def test_failed_or_cancelled_review_preserves_previous_clip(self) -> None:
        output = managed_preview_path(self.project, "review.mp4")
        output.write_bytes(b"previous review")
        for failure in (False, True):

            def render(
                command: list[str], cancel: bool = failure, **options: object
            ) -> subprocess.CompletedProcess[str]:
                self._render(command, **options)
                if cancel:
                    raise RequestCancelled()
                return subprocess.CompletedProcess(command, 1, "", "encoder failed")

            with (
                self.subTest(cancelled=failure),
                patch("shotcut_mcp.render.run_capture", side_effect=render),
                self.assertRaises(ToolError),
            ):
                render_review_clip(self.arguments)
            self.assertEqual(output.read_bytes(), b"previous review")
            self.assertEqual(list(output.parent.glob("*.mp4")), [output])
            self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])

    def test_cancellation_after_render_prevents_promotion(self) -> None:
        event = threading.Event()
        output = managed_preview_path(self.project, "review.mp4")
        output.write_bytes(b"previous review")

        def render(
            command: list[str], **options: object
        ) -> subprocess.CompletedProcess[str]:
            result = self._render(command, **options)
            event.set()
            return result

        with (
            request_cancellation(event),
            patch("shotcut_mcp.render.run_capture", side_effect=render),
            self.assertRaises(RequestCancelled),
        ):
            render_review_clip(self.arguments)
        self.assertEqual(output.read_bytes(), b"previous review")
        self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])

    def test_oversized_review_preserves_previous_clip_and_cleans_candidate(
        self,
    ) -> None:
        output = managed_preview_path(self.project, "review.mp4")
        output.write_bytes(b"previous review")

        def render(
            command: list[str], **_options: object
        ) -> subprocess.CompletedProcess[str]:
            target = Path(
                next(part[9:] for part in command if part.startswith("avformat:"))
            )
            with target.open("wb") as handle:
                handle.truncate(32 * 1024 * 1024 + 1)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch("shotcut_mcp.render.run_capture", side_effect=render),
            self.assertRaises(ToolError) as caught,
        ):
            render_review_clip(self.arguments)
        self.assertEqual(caught.exception.code, "review_clip_size_exceeded")
        self.assertEqual(output.read_bytes(), b"previous review")
        self.assertEqual(list(output.parent.glob("*.mp4")), [output])
        self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])

    def test_review_dimensions_respect_display_aspect_and_do_not_upscale(self) -> None:
        for width, height, numerator, denominator, expected in (
            (640, 480, 4, 3, (640, 480)),
            (720, 576, 16, 9, (720, 404)),
            (1080, 1920, 9, 16, (404, 720)),
        ):
            with (
                self.subTest(width=width, height=height),
                patch(
                    "shotcut_mcp.project.validate_project_file",
                    return_value={"valid": True},
                ),
            ):
                edited = edit_project(
                    {
                        "project_path": str(self.project),
                        "expected_revision": hashlib.sha256(
                            self.project.read_bytes()
                        ).hexdigest(),
                        "operations": [
                            {
                                "op": "set_profile",
                                "preserve_frame_numbers": True,
                                "width": width,
                                "height": height,
                                "display_aspect_num": numerator,
                                "display_aspect_den": denominator,
                            }
                        ],
                    }
                )
            with patch("shotcut_mcp.render.run_capture", side_effect=self._render):
                result = render_review_clip(
                    {**self.arguments, "expected_revision": edited["revision"]}
                )
            self.assertEqual((result["width"], result["height"]), expected)

    def test_project_change_during_review_does_not_change_captured_revision(
        self,
    ) -> None:
        original = self.project.read_bytes()

        def render(
            command: list[str], **options: object
        ) -> subprocess.CompletedProcess[str]:
            self.project.write_bytes(original + b"\n")
            snapshot = next(
                Path(argument) for argument in command if argument.endswith(".mlt")
            )
            self.assertEqual(snapshot.read_bytes(), original)
            return self._render(command, **options)

        with patch("shotcut_mcp.render.run_capture", side_effect=render):
            result = render_review_clip(self.arguments)
        self.assertEqual(result["revision"], self.revision)
        self.assertEqual(self.project.read_bytes(), original + b"\n")

    def test_review_enforces_resource_and_output_policy(self) -> None:
        with (
            patch(
                "shotcut_mcp.render.enforce_project_resource_policy",
                side_effect=ToolError("resource denied"),
            ),
            patch("shotcut_mcp.render.run_capture") as capture,
            self.assertRaises(ToolError),
        ):
            render_review_clip(self.arguments)
        capture.assert_not_called()
        self.assertEqual(list(self.root.glob("*.render-*.mlt")), [])
        with (
            patch.dict(
                "os.environ",
                {"SHOTCUT_MCP_ALLOWED_ROOTS": str(self.root / "elsewhere")},
            ),
            self.assertRaises(ToolError) as caught,
        ):
            render_review_clip(self.arguments)
        self.assertEqual(caught.exception.code, "path_policy_denied")
