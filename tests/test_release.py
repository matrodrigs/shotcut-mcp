from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_release import ROOT, build_release, package_members
from shotcut_mcp.tools import TOOLS


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.version = manifest["version"]

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

    def test_manifest_tool_descriptions_match_runtime_catalog(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        advertised = {tool["name"]: tool["description"] for tool in manifest["tools"]}
        runtime = {tool["name"]: tool["description"] for tool in TOOLS}
        self.assertEqual(advertised, runtime)
        site = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f"<dt>{len(TOOLS)}</dt><dd>MCP tools</dd>", site)
        self.assertIn(f"See all {len(TOOLS)} MCP tools", site)

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


if __name__ == "__main__":
    unittest.main()
