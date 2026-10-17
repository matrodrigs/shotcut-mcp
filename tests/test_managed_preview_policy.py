from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import platform, render
from shotcut_mcp.errors import ToolError
from shotcut_mcp.project import ProjectDocument


class ManagedPreviewPolicyTests(unittest.TestCase):
    def test_directory_alias_outside_allowed_root_is_rejected_before_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory).resolve()
            root = temporary / "allowed"
            outside = temporary / "outside"
            root.mkdir()
            outside.mkdir()
            project = root / "project.mlt"
            document = ProjectDocument.new(
                project, width=640, height=480, fps_num=25, fps_den=1, title="Preview"
            )
            document.apply_operation(
                {
                    "op": "add_generator",
                    "track": "V1",
                    "generator": "color",
                    "duration_frames": 5,
                }
            )
            project.write_bytes(document.to_bytes())
            alias = root / ".shotcut-mcp"
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(alias), str(outside)],
                    check=True,
                    capture_output=True,
                    timeout=10,
                )
            else:
                alias.symlink_to(outside, target_is_directory=True)
            try:
                requests = (
                    lambda: platform.render_preview(project, None, 0, False),
                    lambda: platform.render_contact_sheet(
                        project, None, [0], columns=1, cell_width=320, overwrite=False
                    ),
                    lambda: render.render_review_clip(
                        {"project_path": str(project), "in_frame": 0, "out_frame": 4}
                    ),
                )
                with (
                    patch.dict(os.environ, {"SHOTCUT_MCP_ALLOWED_ROOTS": str(root)}),
                    patch.object(
                        render,
                        "discover_executables",
                        return_value=SimpleNamespace(melt=Path("melt")),
                    ),
                    patch.object(
                        render, "require_executable", return_value=Path("melt")
                    ),
                    patch.object(render, "ensure_melt_ready"),
                ):
                    for index, request in enumerate(requests):
                        with (
                            self.subTest(kind=index),
                            self.assertRaises(ToolError) as caught,
                        ):
                            request()
                        self.assertEqual(caught.exception.code, "path_policy_denied")
                        self.assertEqual(list(outside.iterdir()), [])
                        self.assertEqual(list(root.glob("*.render-*.mlt")), [])
            finally:
                if os.name == "nt":
                    alias.rmdir()
                else:
                    alias.unlink()


if __name__ == "__main__":
    unittest.main()
