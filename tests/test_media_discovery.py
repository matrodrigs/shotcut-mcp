from __future__ import annotations

import os
import tempfile
import threading
import unittest
from contextlib import nullcontext
from itertools import repeat
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp import platform
from shotcut_mcp.errors import RequestCancelled, ToolError
from shotcut_mcp.protocol import request_cancellation


class MediaDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        environment = patch.dict(
            os.environ,
            {
                "SHOTCUT_MCP_ALLOWED_ROOTS": str(self.root),
                "SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES": "0",
                "SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS": "1",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def create_file(self, name: str, data: bytes = b"media") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def discover(self, **arguments: object) -> dict[str, object]:
        return platform.discover_media({"folder_path": str(self.root), **arguments})

    def test_defaults_return_sorted_candidates_without_requiring_ffprobe(self) -> None:
        self.create_file("zebra.MP4")
        self.create_file("Audio.wav")
        self.create_file("cover.jpg")
        self.create_file("readme.txt")
        self.create_file("nested/clip.mov")
        with patch("shotcut_mcp.platform.summarize_media") as probe:
            result = self.discover()
        self.assertEqual(
            [item["relative_path"] for item in result["items"]],
            ["Audio.wav", "cover.jpg", "zebra.MP4"],
        )
        self.assertEqual(
            [item["kind"] for item in result["items"]],
            ["audio", "image", "video"],
        )
        self.assertEqual(result["folder_path"], str(self.root))
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["scanned_entries"], 5)
        self.assertIsNone(result["next_cursor"])
        self.assertTrue(all(item["size_bytes"] == 5 for item in result["items"]))
        self.assertFalse(result["recursive"])
        probe.assert_not_called()

    def test_recursive_pages_are_complete_and_stable_across_limit_changes(self) -> None:
        for name in ("z.mp4", "a/second.mp3", "a/First.png", "b.wav"):
            self.create_file(name)
        first = self.discover(recursive=True, limit=1)
        second = self.discover(recursive=True, limit=2, cursor=first["next_cursor"])
        third = self.discover(recursive=True, limit=1, cursor=second["next_cursor"])
        self.assertEqual(
            [
                item["relative_path"]
                for page in (first, second, third)
                for item in page["items"]
            ],
            ["a/First.png", "a/second.mp3", "b.wav", "z.mp4"],
        )
        self.assertEqual(first["revision"], third["revision"])
        self.assertEqual(second["offset"], 1)
        self.assertEqual(third["offset"], 3)
        self.assertIsNone(third["next_cursor"])

    def test_media_changes_invalidate_a_cursor(self) -> None:
        for change in ("add", "remove", "modify"):
            with self.subTest(change=change):
                first_path = self.create_file("a.mp4")
                self.create_file("b.wav")
                first = self.discover(limit=1)
                if change == "add":
                    self.create_file("c.jpg")
                elif change == "remove":
                    first_path.unlink()
                else:
                    first_path.write_bytes(b"changed")
                with self.assertRaises(ToolError) as caught:
                    self.discover(limit=1, cursor=first["next_cursor"])
                self.assertEqual(
                    caught.exception.code, "media_discovery_revision_conflict"
                )

    def test_cursor_is_bound_to_root_and_recursion(self) -> None:
        self.create_file("a.mp4")
        self.create_file("b.mp4")
        self.create_file("other/a.mp4")
        self.create_file("other/b.mp4")
        first = self.discover(limit=1)
        for arguments in (
            {"recursive": True},
            {"folder_path": str(self.root / "other")},
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(ToolError) as caught,
            ):
                self.discover(cursor=first["next_cursor"], **arguments)
            self.assertEqual(caught.exception.code, "media_discovery_revision_conflict")

    def test_invalid_arguments_and_missing_folder_are_rejected(self) -> None:
        for arguments in (
            {"limit": 0},
            {"limit": 201},
            {"limit": True},
            {"recursive": 1},
            {"include_metadata": "true"},
            {"cursor": "invalid"},
            {"cursor": 20},
            {"folder_path": str(self.root / "missing")},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                self.discover(**arguments)

    def test_metadata_is_compact_and_probe_failure_keeps_other_files(self) -> None:
        self.create_file("a.mp4")
        self.create_file("b.mp4")
        metadata = {
            "duration_seconds": 4.5,
            "streams": [
                {"type": "video", "width": 1920, "height": 1080, "frame_rate": 25.0},
                {"type": "audio", "channels": 2},
            ],
        }
        with patch(
            "shotcut_mcp.platform.summarize_media",
            side_effect=[
                ToolError("Broken file.", code="media_probe_failed"),
                metadata,
            ],
        ) as probe:
            result = self.discover(include_metadata=True)
        self.assertEqual(result["total"], 2)
        self.assertEqual(
            result["items"][0]["probe_error"]["code"], "media_probe_failed"
        )
        self.assertEqual(
            result["items"][1]["metadata"],
            {
                "duration_seconds": 4.5,
                "has_video": True,
                "has_audio": True,
                "width": 1920,
                "height": 1080,
                "frame_rate": 25.0,
                "audio_channels": 2,
            },
        )
        for call in probe.call_args_list:
            self.assertGreater(call.kwargs["timeout_seconds"], 0)
            self.assertLessEqual(call.kwargs["timeout_seconds"], 30)

    def test_metadata_budget_keeps_unprobed_candidates(self) -> None:
        self.create_file("a.mp4")
        self.create_file("b.mp4")
        with (
            patch("shotcut_mcp.platform.time.monotonic", side_effect=[0, 0, 0, 31, 31]),
            patch("shotcut_mcp.platform.summarize_media") as probe,
        ):
            result = self.discover(include_metadata=True)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(
            all(
                item["probe_error"]["code"] == "media_discovery_probe_timeout"
                for item in result["items"]
            )
        )
        probe.assert_not_called()

    def test_root_policy_and_network_denial_precede_scanning(self) -> None:
        for folder, code in (
            (str(self.root.parent), "path_policy_denied"),
            ("//server/share/media", "network_resource_policy_denied"),
            ("https://example.com/media", "network_resource_policy_denied"),
        ):
            with (
                self.subTest(folder=folder),
                patch("shotcut_mcp.platform.os.scandir") as scan,
                self.assertRaises(ToolError) as caught,
            ):
                self.discover(folder_path=folder)
            self.assertEqual(caught.exception.code, code)
            scan.assert_not_called()

    def test_symlink_policy_is_applied_to_targets_and_directories_are_not_followed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "external.mp4"
            external.write_bytes(b"outside")
            internal = self.create_file("inside/real.mp4")
            try:
                (self.root / "external.mp4").symlink_to(external)
                (self.root / "alias.mp4").symlink_to(internal)
                (self.root / "loop").symlink_to(self.root, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Symlinks are unavailable: {exc}")
            result = self.discover(recursive=True)
            self.assertEqual(
                [item["relative_path"] for item in result["items"]],
                ["alias.mp4", "inside/real.mp4"],
            )
            self.assertEqual(result["skipped_entries"], 2)
            self.assertTrue(
                all(item["path"] == str(internal) for item in result["items"])
            )

    def test_network_policy_checks_expanded_and_resolved_paths(self) -> None:
        with (
            patch.dict(os.environ, {"DISCOVERY_TEST_NETWORK": "//server/share/media"}),
            self.assertRaises(ToolError) as caught,
        ):
            self.discover(folder_path="$DISCOVERY_TEST_NETWORK")
        self.assertEqual(caught.exception.code, "network_resource_policy_denied")
        with (
            patch(
                "shotcut_mcp.path_policy.Path.resolve",
                return_value=Path("//server/share/media"),
            ),
            self.assertRaises(ToolError) as caught,
        ):
            self.discover()
        self.assertEqual(caught.exception.code, "network_resource_policy_denied")

    def test_optional_network_policy_preserves_executable_discovery_exemption(
        self,
    ) -> None:
        network_path = Path("//server/share/ffmpeg")
        with patch("shotcut_mcp.path_policy.Path.resolve", return_value=network_path):
            self.assertEqual(
                platform.expand_path(
                    str(network_path), enforce_policy=False, enforce_network_policy=True
                ),
                network_path,
            )

    def test_scan_entry_and_depth_limits_give_narrower_folder_guidance(self) -> None:
        self.create_file("readme.txt")
        with os.scandir(self.root) as entries:
            entry = next(entries)
        with (
            patch(
                "shotcut_mcp.platform.os.scandir",
                return_value=nullcontext(repeat(entry, 20_001)),
            ),
            patch(
                "shotcut_mcp.platform.expand_path",
                side_effect=lambda value, **_: Path(value),
            ),
            patch("shotcut_mcp.platform.time.monotonic", return_value=0),
            self.assertRaises(ToolError) as caught,
        ):
            self.discover()
        self.assertEqual(caught.exception.code, "media_discovery_limit_exceeded")
        self.assertEqual(
            caught.exception.recommended_action, "choose_narrower_folder_and_retry"
        )
        self.create_file("/".join(["sub"] * 17 + ["clip.mp4"]))
        with self.assertRaises(ToolError) as caught:
            self.discover(recursive=True)
        self.assertEqual(caught.exception.code, "media_discovery_limit_exceeded")

    def test_cancellation_during_probe_is_not_reported_as_a_file_error(self) -> None:
        self.create_file("a.mp4")
        with (
            patch(
                "shotcut_mcp.platform.summarize_media", side_effect=RequestCancelled()
            ),
            self.assertRaises(RequestCancelled),
        ):
            self.discover(include_metadata=True)
        cancellation = threading.Event()
        cancellation.set()
        with request_cancellation(cancellation), self.assertRaises(RequestCancelled):
            self.discover()


if __name__ == "__main__":
    unittest.main()
