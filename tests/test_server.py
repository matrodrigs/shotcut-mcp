from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).parents[1] / "scripts" / "shotcut_mcp_server.py"
SPEC = importlib.util.spec_from_file_location("shotcut_mcp_server", SERVER_PATH)
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


class ShotcutMcpTests(unittest.TestCase):
    def test_protocol_initialize_and_tools_list(self) -> None:
        messages = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                "",
            ]
        )
        result = subprocess.run(
            [sys.executable, str(SERVER_PATH)],
            input=messages,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        names = {item["name"] for item in responses[1]["result"]["tools"]}
        self.assertIn("create_project", names)
        self.assertIn("start_render", names)

    def test_create_and_inspect_project_without_overwriting(self) -> None:
        fake_probe = {
            "format": {"duration": "2.0", "format_name": "fake"},
            "streams": [{"codec_type": "video", "duration": "2.0"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"fake")
            project = root / "timeline.mlt"
            with patch.object(server, "_probe_media_raw", return_value=fake_probe):
                created = server.tool_create_project(
                    {
                        "project_path": str(project),
                        "clips": [{"path": str(media), "in_seconds": 0.5, "out_seconds": 1.5}],
                        "fps_num": 30,
                    }
                )
            self.assertEqual(created["duration_frames"], 30)
            inspected = server.inspect_project_file(project)
            self.assertTrue(inspected["shotcut_editable"])
            self.assertEqual(inspected["tracks"][0]["entries"], 1)
            self.assertEqual(inspected["missing_resources"], [])
            with patch.object(server, "_probe_media_raw", return_value=fake_probe):
                with self.assertRaises(server.ToolError):
                    server.tool_create_project(
                        {"project_path": str(project), "clips": [{"path": str(media)}]}
                    )


if __name__ == "__main__":
    unittest.main()
