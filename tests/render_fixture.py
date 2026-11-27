from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree.ElementTree import Element

from shotcut_mcp.render import start_render


def start_with_python_renderer(
    renderer_path: Path, output_path: Path, **arguments: object
) -> dict[str, object]:
    source = renderer_path.read_bytes()
    render_input = SimpleNamespace(
        source=source,
        revision=hashlib.sha256(source).hexdigest(),
        fps=30.0,
        duration_frames=100,
        markers=[],
    )
    with (
        patch(
            "shotcut_mcp.render.load_project_render_input",
            return_value=render_input,
        ),
        patch(
            "shotcut_mcp.render.discover_executables",
            return_value=SimpleNamespace(melt=Path(sys.executable)),
        ),
        patch(
            "shotcut_mcp.render.require_executable",
            return_value=Path(sys.executable),
        ),
        patch("shotcut_mcp.render.ensure_melt_ready"),
        # Python stands in for Melt. Its script is the snapshot payload in these process tests.
        patch(
            "shotcut_mcp.path_policy.parsed_project_resources",
            return_value=(Element("mlt"), []),
        ),
    ):
        return start_render(
            {
                "project_path": str(renderer_path),
                "output_path": str(output_path),
                **arguments,
            }
        )
