from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.build_release import ROOT, build_release, package_members
from scripts.check_release import (
    runtime_tool_entries,
    sync_tool_contracts,
    validate_client_adapters,
    validate_tool_contracts,
)
from scripts.require_green_ci import require_green_ci


class ReleaseBundleTests(unittest.TestCase):
    CLIENT_ADAPTER_FILES = (
        Path(".codex-plugin/plugin.json"),
        Path(".claude-plugin/plugin.json"),
        Path(".claude-plugin/marketplace.json"),
        Path(".mcp.json"),
        Path("manifest.json"),
        Path("scripts/shotcut_mcp_server.py"),
    )

    def setUp(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.version = manifest["version"]

    def _copy_client_adapter_fixture(self, root: Path) -> None:
        for relative in self.CLIENT_ADAPTER_FILES:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)

    def test_release_bundle_is_reproducible_and_contains_only_runtime_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_release(self.version, root / "first")
            second = build_release(self.version, root / "second")
            first_artifact = Path(first["artifact"])
            second_artifact = Path(second["artifact"])

            self.assertEqual(first_artifact.read_bytes(), second_artifact.read_bytes())
            self.assertEqual(first["digest"], second["digest"])
            expected = sorted(path.as_posix() for path in package_members())
            with zipfile.ZipFile(first_artifact) as bundle:
                self.assertEqual(sorted(bundle.namelist()), expected)
                self.assertNotIn("server.json", bundle.namelist())
                self.assertNotIn("tests/test_release.py", bundle.namelist())
                self.assertNotIn(".claude-plugin/plugin.json", bundle.namelist())
                self.assertNotIn(".claude-plugin/marketplace.json", bundle.namelist())
                self.assertNotIn(".codex-plugin/plugin.json", bundle.namelist())
                extracted = root / "extracted"
                bundle.extractall(extracted)
            checksum = Path(first["checksum"]).read_text(encoding="ascii")
            self.assertEqual(checksum, f"{first['digest']}  {first_artifact.name}\n")
            self.assertTrue(Path(first["notes"]).read_text(encoding="utf-8").strip())

            messages = "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-11-25",
                                "capabilities": {},
                                "clientInfo": {"name": "release-test", "version": "1"},
                            },
                        }
                    ),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    "",
                ]
            )
            result = subprocess.run(
                [sys.executable, str(extracted / "scripts/shotcut_mcp_server.py")],
                input=messages.encode("utf-8"),
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(
                result.returncode, 0, result.stderr.decode(errors="replace")
            )
            responses = [
                json.loads(line) for line in result.stdout.decode("utf-8").splitlines()
            ]
            self.assertEqual(
                responses[0]["result"]["serverInfo"]["version"], self.version
            )
            manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                len(responses[1]["result"]["tools"]), len(manifest["tools"])
            )

    def test_checked_in_tool_contracts_match_the_runtime_projection(self) -> None:
        validate_tool_contracts(ROOT, runtime_tool_entries())
        self.assertEqual(sync_tool_contracts(ROOT, runtime_tool_entries()), ())

    def test_tool_contract_sync_updates_only_mechanical_projections(self) -> None:
        entries = [
            {"name": "first_tool", "description": "First runtime description."},
            {"name": "second_tool", "description": "Second runtime description."},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "fixture",
                        "tools": list(reversed(entries)),
                        "tools_generated": False,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            readme = (
                "# Fixture\n\n"
                "## MCP tools\n\n"
                "| Tool | Purpose |\n"
                "| --- | --- |\n"
                "| `first_tool` | Concise human summary |\n"
                "| `second_tool` | Another human summary |\n"
            )
            (root / "README.md").write_text(readme, encoding="utf-8")
            (root / "docs" / "index.html").write_text(
                "<dt>1</dt><dd>MCP tools</dd>\nSee all 1 MCP tools\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "order_changed=True"):
                validate_tool_contracts(root, entries)

            changed = sync_tool_contracts(root, entries)

            self.assertEqual(
                changed,
                (root / "manifest.json", root / "docs" / "index.html"),
            )
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["tools"], entries)
            self.assertIs(manifest["tools_generated"], False)
            self.assertEqual((root / "README.md").read_text(encoding="utf-8"), readme)
            self.assertIn(
                "<dt>2</dt><dd>MCP tools</dd>",
                (root / "docs" / "index.html").read_text(encoding="utf-8"),
            )
            validate_tool_contracts(root, entries)
            self.assertEqual(sync_tool_contracts(root, entries), ())

    def test_client_adapters_match_runtime_release_and_entry_point(self) -> None:
        validate_client_adapters(ROOT, self.version)

    def test_client_adapter_validation_rejects_entry_point_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_client_adapter_fixture(root)

            config_path = root / ".codex-plugin" / "plugin.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["mcpServers"]["shotcut"]["args"] = ["./scripts/other_server.py"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Codex launcher"):
                validate_client_adapters(root, self.version)

    def test_client_adapter_validation_rejects_marketplace_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_client_adapter_fixture(root)

            catalog_path = root / ".claude-plugin" / "marketplace.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["plugins"][0]["source"] = "./other-plugin"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "marketplace catalog"):
                validate_client_adapters(root, self.version)

    def test_release_bundle_rejects_a_version_mismatch(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(RuntimeError, "does not match manifest"),
        ):
            build_release("0.0.0", Path(directory))

    def test_release_bundle_rejects_a_non_release_version(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(RuntimeError, "must be X.Y.Z"),
        ):
            build_release("next", Path(directory))


class SiteAssetTests(unittest.TestCase):
    def test_mobile_video_idle_detection_covers_touch_capable_browsers(self) -> None:
        site_script = (ROOT / "docs" / "site.js").read_text(encoding="utf-8")
        site_styles = (ROOT / "docs" / "styles.css").read_text(encoding="utf-8")

        self.assertIn(
            'window.matchMedia("(any-pointer: coarse)")',
            site_script,
        )
        self.assertIn("navigator.maxTouchPoints > 0", site_script)
        self.assertNotIn(
            'window.matchMedia("(hover: none) and (pointer: coarse)")',
            site_script,
        )
        self.assertIn(
            "\n.demo-video-shell.has-custom-controls.is-playing.is-controls-idle "
            ".demo-controls {\n",
            site_styles,
        )


class ReleaseCiGateTests(unittest.TestCase):
    def test_successful_main_push_ci_allows_release(self) -> None:
        runs = [
            {
                "databaseId": 1,
                "headBranch": "feature",
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-22T12:00:00Z",
            },
            {
                "databaseId": 2,
                "headBranch": "main",
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-22T13:00:00Z",
                "url": "https://github.example/actions/runs/2",
            },
        ]
        with patch("scripts.require_green_ci._list_ci_runs", return_value=runs):
            run = require_green_ci("owner/repository", "a" * 40, timeout_seconds=0)

        self.assertEqual(run["databaseId"], 2)

    def test_failed_main_push_ci_blocks_release(self) -> None:
        runs = [
            {
                "databaseId": 3,
                "headBranch": "main",
                "status": "completed",
                "conclusion": "failure",
                "createdAt": "2026-07-22T13:00:00Z",
                "url": "https://github.example/actions/runs/3",
            }
        ]
        with (
            patch("scripts.require_green_ci._list_ci_runs", return_value=runs),
            self.assertRaisesRegex(RuntimeError, "concluded failure"),
        ):
            require_green_ci("owner/repository", "b" * 40, timeout_seconds=0)

    def test_missing_main_push_ci_blocks_release(self) -> None:
        with (
            patch("scripts.require_green_ci._list_ci_runs", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "latest state: not found"),
        ):
            require_green_ci("owner/repository", "c" * 40, timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
