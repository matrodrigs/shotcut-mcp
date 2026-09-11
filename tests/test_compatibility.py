from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.compatibility_matrix import integration_matrix
from shotcut_mcp import MLT_VERSION, SHOTCUT_VERSION, TESTED_RUNTIME_STACKS, platform
from shotcut_mcp.errors import ToolError
from shotcut_mcp.project import ProjectDocument
from shotcut_mcp.protocol import schema_errors
from shotcut_mcp.server import (
    SUPPORTED_PROTOCOL_VERSIONS,
    ProtocolSession,
    handle_request,
)
from shotcut_mcp.tools import TOOLS


class CompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.versions = self.enter_patch(
            "version_line", side_effect=["Shotcut 26.8.1", "melt 7.41.0"]
        )
        self.executables = self.enter_patch(
            "discover_executables",
            return_value=platform.Executables(
                Path("shotcut"), Path("melt"), Path("ffprobe"), Path("ffmpeg")
            ),
        )
        self.repository = self.enter_patch("ensure_melt_ready")
        self.services = self.enter_patch(
            "describe_service",
            side_effect=lambda kind, name: {
                "kind": kind,
                "name": name,
                "available": True,
            },
        )
        self.enter_patch("quality_analyzer_capabilities", return_value={})

    def enter_patch(self, name, **kwargs):
        patcher = patch(f"shotcut_mcp.platform.{name}", **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def call_doctor(self, version="2025-11-25"):
        result = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "shotcut_doctor", "arguments": {}},
            },
            ProtocolSession(protocol_version=version),
        )["result"]
        self.assertFalse(result.get("isError"), result)
        report = (
            result["structuredContent"]
            if "structuredContent" in result
            else json.loads(result["content"][0]["text"])
        )
        schema = next(
            tool["outputSchema"] for tool in TOOLS if tool["name"] == "shotcut_doctor"
        )
        self.assertEqual(schema_errors(report, schema), [])
        if version in {"2024-11-05", "2025-03-26"}:
            self.assertNotIn("structuredContent", result)
        else:
            self.assertEqual(result["structuredContent"], report)
            summary = result["content"][0]["text"].lower()
            expected = {
                "tested": "runtime checks passed",
                "untested": "pair is untested",
                "failed": "runtime checks failed",
            }
            self.assertIn(expected[report["status"]], summary)
        return report

    def test_editing_an_older_project_preserves_its_serialization_metadata(
        self,
    ) -> None:
        fixture = Path(__file__).parent / "fixtures/shotcut-26.6/multitrack-ripple.mlt"
        document = ProjectDocument.load(fixture)
        document.apply_operation(
            {"op": "add_track", "kind": "video", "name": "Compatibility"}
        )
        self.assertEqual(document.root.get("version"), "7.40.0")
        self.assertEqual(document.root.get("title"), "Shotcut version 26.6.25")

    def test_reported_shotcut_upgrade_is_recognized(self) -> None:
        result = self.call_doctor()
        self.assertTrue(result["compatible"], result)
        self.assertEqual(result["status"], "tested")
        self.assertTrue(result["runtime_ready"])
        self.assertEqual(result["issues"], [])

    def test_tested_pairs_work_across_all_protocol_versions(self) -> None:
        for protocol in SUPPORTED_PROTOCOL_VERSIONS:
            for shotcut, mlt in TESTED_RUNTIME_STACKS:
                with self.subTest(protocol=protocol, shotcut=shotcut, mlt=mlt):
                    self.versions.side_effect = [f"Shotcut {shotcut}", f"melt {mlt}"]
                    result = self.call_doctor(protocol)
                    self.assertTrue(result["compatible"])
                    self.assertEqual(result["status"], "tested")
                    self.assertEqual(
                        result["serialization"],
                        {"shotcut": SHOTCUT_VERSION, "mlt": MLT_VERSION},
                    )

    def test_unknown_versions_patches_and_mixed_pairs_are_warnings(self) -> None:
        for shotcut, mlt in (
            ("26.9.1", "7.42.0"),
            ("26.8.1", "7.41.1"),
            ("26.8.1-dev", "7.41.0"),
            ("26.8.1", "7.41.0.1"),
            ("26.8.1", "7.41.0-rc1"),
            ("26.8.1", "7.40.0"),
            ("26.6.25", "7.41.0"),
            ("development build", "7.41.0"),
        ):
            with self.subTest(shotcut=shotcut, mlt=mlt):
                self.versions.side_effect = [f"Shotcut {shotcut}", f"melt {mlt}"]
                result = self.call_doctor()
                self.assertEqual(result["status"], "untested")
                self.assertTrue(result["runtime_ready"])
                self.assertFalse(result["compatible"])
                self.assertEqual(
                    [issue["code"] for issue in result["issues"]], ["untested_runtime"]
                )
                self.assertEqual(result["issues"][0]["severity"], "warning")

    def test_repository_failure_takes_precedence_over_unknown_version(self) -> None:
        self.versions.side_effect = ["Shotcut 26.9.1", "melt 7.42.0"]
        self.repository.side_effect = ToolError("repository failure sentinel")
        result = self.call_doctor()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["compatible"])
        self.assertFalse(result["runtime_ready"])
        self.assertEqual(result["issues"][0]["code"], "repository_unavailable")
        self.assertIn("repository failure sentinel", result["issues"][0]["message"])
        self.assertTrue(result["issues"][0]["recommended_action"])

    def test_rnnoise_variants_are_independent_and_either_can_pass(self) -> None:
        for available in ({"link"}, {"filter"}, set()):
            with self.subTest(available=available):
                self.versions.side_effect = ["Shotcut 26.8.1", "melt 7.41.0"]
                self.services.side_effect = lambda kind, name, available=available: {
                    "kind": kind,
                    "name": name,
                    "available": kind in available,
                }
                result = self.call_doctor()
                self.assertEqual(
                    set(result["checks"]["rnnoise"]["services"]), {"link", "filter"}
                )
                self.assertEqual(result["status"], "tested" if available else "failed")
                if not available:
                    self.assertEqual(result["issues"][0]["code"], "rnnoise_unavailable")

    def test_missing_executables_and_failed_version_queries_are_errors(self) -> None:
        for shotcut, mlt, versions, expected in (
            (None, Path("melt"), [None, "melt 7.41.0"], "shotcut_unavailable"),
            (Path("shotcut"), None, ["Shotcut 26.8.1", None], "mlt_unavailable"),
            (
                Path("shotcut"),
                Path("melt"),
                [ToolError("version query failed"), "melt 7.41.0"],
                "shotcut_unavailable",
            ),
            (Path("shotcut"), Path("melt"), ["", "melt 7.41.0"], "shotcut_unavailable"),
        ):
            with self.subTest(expected=expected, versions=versions):
                self.executables.return_value = platform.Executables(
                    shotcut, mlt, None, None
                )
                self.versions.side_effect = versions
                result = self.call_doctor()
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["runtime_ready"])
                self.assertFalse(result["compatible"])
                self.assertIn(expected, {issue["code"] for issue in result["issues"]})

    def test_ci_matrix_matches_advertised_pairs_and_pins_every_archive(self) -> None:
        root = Path(__file__).resolve().parents[1]
        process = subprocess.run(
            [sys.executable, "-B", str(root / "scripts/compatibility_matrix.py")],
            cwd=root.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        matrix = json.loads(process.stdout)["include"]
        report = self.call_doctor()
        self.assertEqual(
            [{"shotcut": row["shotcut"], "mlt": row["mlt"]} for row in matrix],
            report["tested_stacks"],
        )
        self.assertIn((SHOTCUT_VERSION, MLT_VERSION), TESTED_RUNTIME_STACKS)
        self.assertEqual(len(TESTED_RUNTIME_STACKS), len(set(TESTED_RUNTIME_STACKS)))
        for row in matrix:
            self.assertRegex(row["sha256"], r"^[a-f0-9]{64}$")
        workflow = (root / ".github/workflows/shotcut-integration.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python -B scripts/compatibility_matrix.py", workflow)
        self.assertIn("fromJSON(needs.runtime-matrix.outputs.matrix)", workflow)
        self.assertIn("SHOTCUT_MCP_EXPECTED_SHOTCUT: ${{ matrix.shotcut }}", workflow)
        self.assertIn("SHOTCUT_MCP_EXPECTED_MLT: ${{ matrix.mlt }}", workflow)

    def test_ci_rejects_an_advertised_version_without_a_pinned_archive(self) -> None:
        with (
            patch(
                "scripts.compatibility_matrix.TESTED_RUNTIME_STACKS",
                (*TESTED_RUNTIME_STACKS, ("26.9.1", "7.42.0")),
            ),
            self.assertRaisesRegex(RuntimeError, "pinned Windows archives"),
        ):
            integration_matrix()
