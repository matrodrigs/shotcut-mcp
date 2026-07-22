from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp.errors import ToolError
from shotcut_mcp.platform import render_preview
from shotcut_mcp.render import start_render


class PreviewSafetyTests(unittest.TestCase):
    @staticmethod
    def _platform_patches(render: object) -> tuple[object, ...]:
        return (
            patch(
                "shotcut_mcp.platform.discover_executables",
                return_value=SimpleNamespace(melt=Path("melt")),
            ),
            patch("shotcut_mcp.platform.require_executable", return_value=Path("melt")),
            patch("shotcut_mcp.platform.ensure_melt_ready"),
            patch("shotcut_mcp.platform.run_capture", side_effect=render),
        )

    def test_preview_rejects_the_project_as_its_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            original = b"<mlt/>\n"
            project_path.write_bytes(original)

            def render(command: list[str], **_kwargs: object) -> SimpleNamespace:
                target = next(
                    value.removeprefix("avformat:")
                    for value in command
                    if value.startswith("avformat:")
                )
                Path(target).write_bytes(b"PNG")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            first, second, third, fourth = self._platform_patches(render)
            with first, second, third, fourth:
                with self.assertRaises(ToolError):
                    render_preview(project_path, project_path, frame=0, overwrite=True)

            self.assertEqual(project_path.read_bytes(), original)

    def test_failed_preview_preserves_an_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            output_path = Path(directory) / "preview.png"
            project_path.write_text("<mlt/>\n", encoding="utf-8")
            output_path.write_bytes(b"existing")

            def fail_after_partial_output(
                command: list[str], **_kwargs: object
            ) -> SimpleNamespace:
                target = next(
                    value.removeprefix("avformat:")
                    for value in command
                    if value.startswith("avformat:")
                )
                Path(target).write_bytes(b"partial")
                return SimpleNamespace(returncode=1, stdout="", stderr="failed")

            first, second, third, fourth = self._platform_patches(
                fail_after_partial_output
            )
            with first, second, third, fourth:
                with self.assertRaises(ToolError):
                    render_preview(project_path, output_path, frame=0, overwrite=True)

            self.assertEqual(output_path.read_bytes(), b"existing")
            self.assertEqual(list(Path(directory).glob("*.tmp.png")), [])


class RenderPropertySafetyTests(unittest.TestCase):
    def test_sidecar_consumer_properties_are_rejected_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            output_path = Path(directory) / "output.mp4"
            escaped_path = Path(directory) / "escaped-%03d.ts"
            project_path.write_text("<mlt/>\n", encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "safe allowlist"):
                start_render(
                    {
                        "project_path": str(project_path),
                        "output_path": str(output_path),
                        "consumer_properties": {
                            "f": "hls",
                            "hls_segment_filename": str(escaped_path),
                        },
                    }
                )


if __name__ == "__main__":
    unittest.main()
