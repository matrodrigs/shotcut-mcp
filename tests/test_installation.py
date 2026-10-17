from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.shotcut_mcp_server import main
from shotcut_mcp.errors import ToolError


class InstallationCheckTests(unittest.TestCase):
    def test_check_reports_interpreter_and_ready_runtime_without_starting_stdio(
        self,
    ) -> None:
        runtime = {"melt": {"path": "/tools/melt"}}
        doctor = {"runtime_ready": True, "status": "untested", "issues": []}
        output = io.StringIO()
        with (
            patch("scripts.shotcut_mcp_server.status", return_value=runtime),
            patch(
                "scripts.shotcut_mcp_server.compatibility_doctor", return_value=doctor
            ),
            patch("scripts.shotcut_mcp_server.serve_main") as server,
            redirect_stdout(output),
        ):
            self.assertEqual(main(["--check"]), 0)

        report = json.loads(output.getvalue())
        self.assertEqual(report["python"]["executable"], sys.executable)
        self.assertEqual(report["python"]["version"], sys.version.split()[0])
        self.assertEqual(report["runtime"], runtime)
        self.assertEqual(report["doctor"], doctor)
        self.assertTrue(report["runtime_ready"])
        server.assert_not_called()

    def test_check_returns_failure_for_runtime_errors(self) -> None:
        doctor = {
            "runtime_ready": False,
            "status": "failed",
            "issues": [{"code": "mlt_unavailable"}],
        }
        output = io.StringIO()
        with (
            patch("scripts.shotcut_mcp_server.status", return_value={}),
            patch(
                "scripts.shotcut_mcp_server.compatibility_doctor", return_value=doctor
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["--check"]), 1)

        report = json.loads(output.getvalue())
        self.assertFalse(report["runtime_ready"])
        self.assertEqual(report["doctor"]["issues"], doctor["issues"])

    def test_default_entrypoint_preserves_stdio_server(self) -> None:
        with patch("scripts.shotcut_mcp_server.serve_main", return_value=0) as server:
            self.assertEqual(main([]), 0)
        server.assert_called_once_with()

    def test_check_preserves_interpreter_details_when_discovery_fails(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "scripts.shotcut_mcp_server.status",
                side_effect=ToolError("Invalid root"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["--check"]), 1)

        report = json.loads(output.getvalue())
        self.assertEqual(report["python"]["executable"], sys.executable)
        self.assertEqual(report["error"], "Invalid root")
        self.assertFalse(report["runtime_ready"])


if __name__ == "__main__":
    unittest.main()
