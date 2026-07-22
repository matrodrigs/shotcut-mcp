from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp import platform as platform_module
from shotcut_mcp.errors import ConflictError, ToolError
from shotcut_mcp.project import (
    ProjectDocument,
    create_project,
    edit_project,
    list_backups,
)
from shotcut_mcp.render import start_render

PLUGIN_ROOT = Path(__file__).parents[1]
SERVER_PATH = PLUGIN_ROOT / "scripts" / "shotcut_mcp_server.py"


class MeltStartupTests(unittest.TestCase):
    def tearDown(self) -> None:
        cache = getattr(platform_module, "_MELT_READY_CACHE", None)
        if cache is not None:
            cache.clear()

    def test_cold_start_timeout_is_retried_once_and_then_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            melt = Path(directory) / "melt.exe"
            melt.write_bytes(b"test executable")
            timeout = ToolError("cold start timed out")
            timeout.__cause__ = subprocess.TimeoutExpired([str(melt)], 5)
            success = subprocess.CompletedProcess(
                [str(melt), "-query", "consumers"], 0, "consumers", ""
            )

            with patch(
                "shotcut_mcp.platform.run_capture",
                side_effect=[timeout, success],
            ) as run:
                platform_module.ensure_melt_ready(melt, attempts=3, timeout=5)
                platform_module.ensure_melt_ready(melt, attempts=3, timeout=5)

            self.assertEqual(run.call_count, 2)
            run.assert_called_with(
                [str(melt), "-query", "consumers"], timeout=5
            )


class ProtocolTests(unittest.TestCase):
    def test_initialize_and_list_tools_are_utf8_json_lines(self) -> None:
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
            input=messages.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        responses = [
            json.loads(line) for line in result.stdout.decode("utf-8").splitlines()
        ]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        names = {item["name"] for item in responses[1]["result"]["tools"]}
        self.assertEqual(len(names), 18)
        self.assertIn("edit_project", names)
        self.assertIn("shotcut_capabilities", names)
        self.assertIn("restore_project_backup", names)


class RenderSafetyTests(unittest.TestCase):
    def test_invalid_overwrite_render_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project.mlt"
            output = root / "important.mp4"
            project.write_text("<mlt/>", encoding="utf-8")
            output.write_bytes(b"existing-output")
            with self.assertRaises(ToolError):
                start_render(
                    {
                        "project_path": str(project),
                        "output_path": str(output),
                        "preset": "does-not-exist",
                        "overwrite": True,
                    }
                )
            self.assertEqual(output.read_bytes(), b"existing-output")


class ProjectEditingTests(unittest.TestCase):
    def setUp(self) -> None:
        validation = patch(
            "shotcut_mcp.project.validate_project_file",
            return_value={"valid": True, "validator": "test"},
        )
        self.validate_mock = validation.start()
        self.addCleanup(validation.stop)

    def create_empty(self, root: Path) -> dict:
        return create_project(
            {
                "project_path": str(root / "project.mlt"),
                "width": 640,
                "height": 360,
                "fps_num": 30,
                "validate": False,
            }
        )

    def test_batch_edit_builds_multitrack_text_markers_subtitles_and_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            edited = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "validate": False,
                    "operations": [
                        {"op": "add_track", "kind": "audio", "name": "Narration"},
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "text",
                            "text": "Olá, Shotcut",
                            "duration_frames": 90,
                            "position_frame": 0,
                            "mode": "overwrite",
                        },
                        {
                            "op": "add_generator",
                            "track": "Narration",
                            "generator": "tone",
                            "duration_frames": 90,
                            "position_frame": 0,
                            "mode": "overwrite",
                        },
                        {
                            "op": "add_marker",
                            "start_frame": 15,
                            "end_frame": 30,
                            "text": "Intro",
                        },
                        {"op": "set_notes", "notes": "Projeto completo"},
                        {
                            "op": "set_subtitle_track",
                            "name": "Português",
                            "language": "por",
                            "burn_in": True,
                            "items": [{"start_ms": 0, "end_ms": 1000, "text": "Olá"}],
                        },
                    ],
                }
            )
            project = edited["project"]
            self.assertEqual(
                [track["name"] for track in project["tracks"]], ["V1", "Narration"]
            )
            self.assertEqual(project["duration_frames"], 90)
            self.assertEqual(project["notes"], "Projeto completo")
            self.assertEqual(project["markers"][0]["text"], "Intro")
            self.assertEqual(project["subtitles"][0]["name"], "Português")
            self.assertEqual(project["tracks"][0]["items"][0]["caption"], None)
            backups = list_backups(Path(created["path"]))
            self.assertEqual(backups["backup_count"], 1)

    def test_stale_revision_is_rejected_without_changing_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            first = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "validate": False,
                    "operations": [{"op": "set_notes", "notes": "first"}],
                }
            )
            with self.assertRaises(ConflictError):
                edit_project(
                    {
                        "project_path": created["path"],
                        "expected_revision": created["revision"],
                        "validate": False,
                        "operations": [{"op": "set_notes", "notes": "stale"}],
                    }
                )
            current = ProjectDocument.load(Path(created["path"]))
            self.assertEqual(current.revision, first["revision"])
            self.assertEqual(current.snapshot()["notes"], "first")

    def test_transition_round_trip_restores_two_clips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            edited = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "validate": False,
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "color": "#FF0000",
                            "duration_frames": 30,
                            "position_frame": 0,
                            "mode": "overwrite",
                        },
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "color": "#0000FF",
                            "duration_frames": 30,
                        },
                        {
                            "op": "add_transition",
                            "track": "V1",
                            "left_item_index": 0,
                            "duration_frames": 10,
                        },
                    ],
                }
            )
            items = edited["project"]["tracks"][0]["items"]
            self.assertEqual(
                [item["type"] for item in items], ["clip", "transition", "clip"]
            )
            self.assertEqual(edited["project"]["duration_frames"], 50)
            removed = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": edited["revision"],
                    "validate": False,
                    "operations": [
                        {"op": "remove_transition", "track": "V1", "item_index": 1}
                    ],
                }
            )
            items = removed["project"]["tracks"][0]["items"]
            self.assertEqual([item["duration_frames"] for item in items], [30, 30])
            self.assertEqual(removed["project"]["duration_frames"], 60)

    def test_first_appended_item_replaces_empty_track_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            edited = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "validate": False,
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "color": "#000000",
                            "duration_frames": 30,
                        }
                    ],
                }
            )
            items = edited["project"]["tracks"][0]["items"]
            self.assertEqual([item["type"] for item in items], ["clip"])
            self.assertEqual(edited["project"]["duration_frames"], 30)

    def test_generated_service_precedes_referencing_track_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            edited = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "validate": False,
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "color": "#3366CC",
                            "duration_frames": 30,
                            "position_frame": 0,
                            "mode": "overwrite",
                        }
                    ],
                }
            )
            document = ProjectDocument.load(Path(edited["path"]))
            children = list(document.root)
            playlist = document.find_track("V1").playlist
            entry = playlist.find("entry")
            self.assertIsNotNone(entry)
            producer = document.id_map()[entry.get("producer", "")]
            self.assertLess(children.index(producer), children.index(playlist))

    def test_edit_repairs_forward_referenced_timeline_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            edited = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "validate": False,
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 30,
                            "position_frame": 0,
                            "mode": "overwrite",
                        }
                    ],
                }
            )
            document = ProjectDocument.load(Path(edited["path"]))
            playlist = document.find_track("V1").playlist
            entry = playlist.find("entry")
            self.assertIsNotNone(entry)
            producer = document.id_map()[entry.get("producer", "")]
            document.root.remove(producer)
            document.root.insert(list(document.root).index(document.main_tractor()), producer)
            Path(edited["path"]).write_bytes(document.to_bytes())
            broken = ProjectDocument.load(Path(edited["path"]))
            repaired = edit_project(
                {
                    "project_path": edited["path"],
                    "expected_revision": broken.revision,
                    "validate": False,
                    "operations": [{"op": "set_notes", "notes": "repair"}],
                }
            )
            fixed = ProjectDocument.load(Path(repaired["path"]))
            children = list(fixed.root)
            playlist = fixed.find_track("V1").playlist
            producer = fixed.id_map()[playlist.find("entry").get("producer", "")]
            self.assertLess(children.index(producer), children.index(playlist))

    def test_validation_cannot_be_disabled_by_tool_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            created = create_project(
                {
                    "project_path": str(Path(directory) / "always-validates.mlt"),
                    "validate": False,
                }
            )
            edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "validate": False,
                    "operations": [{"op": "set_notes", "notes": "validated"}],
                }
            )
            self.assertEqual(self.validate_mock.call_count, 2)

    def test_string_force_cannot_bypass_revision_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            created = self.create_empty(Path(directory))
            with self.assertRaisesRegex(ToolError, "force must be a boolean"):
                edit_project(
                    {
                        "project_path": created["path"],
                        "force": "false",
                        "operations": [{"op": "set_notes", "notes": "unsafe"}],
                    }
                )
            self.assertEqual(
                ProjectDocument.load(Path(created["path"])).snapshot()["notes"], ""
            )

    def test_disabling_subtitle_burn_removes_the_render_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            created = self.create_empty(Path(directory))
            burned = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "set_subtitle_track",
                            "name": "Português",
                            "burn_in": True,
                            "items": [{"start_ms": 0, "end_ms": 1000, "text": "Olá"}],
                        }
                    ],
                }
            )
            unburned = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": burned["revision"],
                    "operations": [
                        {
                            "op": "set_subtitle_track",
                            "name": "Português",
                            "burn_in": False,
                            "items": [{"start_ms": 0, "end_ms": 1000, "text": "Olá"}],
                        }
                    ],
                }
            )
            document = ProjectDocument.load(Path(unburned["path"]))
            services = [
                next(
                    (
                        prop.text
                        for prop in item.findall("property")
                        if prop.get("name") == "mlt_service"
                    ),
                    None,
                )
                for item in document.main_tractor().findall("filter")
            ]
            self.assertIn("subtitle_feed", services)
            self.assertNotIn("subtitle", services)

    def test_unknown_transition_layout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            edited = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 30,
                        },
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 30,
                        },
                        {
                            "op": "add_transition",
                            "track": "V1",
                            "left_item_index": 0,
                            "duration_frames": 10,
                        },
                    ],
                }
            )
            path = Path(edited["path"])
            tree = ET.parse(path)
            transition = next(
                item
                for item in tree.getroot().findall("tractor")
                if any(
                    prop.get("name") == "shotcut:transition"
                    for prop in item.findall("property")
                )
            )
            ET.SubElement(transition, "track", {"producer": "unexpected"})
            tree.write(path, encoding="utf-8", xml_declaration=True)
            current = ProjectDocument.load(path)
            with self.assertRaisesRegex(ToolError, "not recognized"):
                edit_project(
                    {
                        "project_path": str(path),
                        "expected_revision": current.revision,
                        "operations": [
                            {
                                "op": "remove_transition",
                                "track": "V1",
                                "item_index": 1,
                            }
                        ],
                    }
                )

    def test_basename_relink_requires_unambiguous_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            edited = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 10,
                        },
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 10,
                        },
                    ],
                }
            )
            path = Path(edited["path"])
            tree = ET.parse(path)
            producers = [
                producer
                for producer in tree.getroot().findall("producer")
                if producer.get("id", "").startswith("producer_")
            ]
            for producer, resource in zip(
                producers, ("C:/one/clip.mp4", "D:/two/clip.mp4"), strict=True
            ):
                prop = next(
                    item
                    for item in producer.findall("property")
                    if item.get("name") == "resource"
                )
                prop.text = resource
            tree.write(path, encoding="utf-8", xml_declaration=True)
            target = root / "replacement.mp4"
            target.write_bytes(b"target")
            current = ProjectDocument.load(path)
            with self.assertRaisesRegex(ToolError, "matches 2 resources"):
                edit_project(
                    {
                        "project_path": str(path),
                        "expected_revision": current.revision,
                        "operations": [
                            {
                                "op": "relink_media",
                                "from": "clip.mp4",
                                "to": str(target),
                                "match_basename": True,
                            }
                        ],
                    }
                )

    def test_profile_change_preserves_clock_encoded_frame_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = create_project(
                {"project_path": str(root / "fps.mlt"), "fps_num": 25}
            )
            edited = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 250,
                        },
                        {"op": "add_marker", "start_frame": 200},
                    ],
                }
            )
            path = Path(edited["path"])
            tree = ET.parse(path)
            entry = tree.getroot().find("playlist[@id='playlist_v1']/entry")
            assert entry is not None
            entry.set("in", "00:00:00.000")
            entry.set("out", "00:00:09.960")
            tree.write(path, encoding="utf-8", xml_declaration=True)
            current = ProjectDocument.load(path)
            changed = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": current.revision,
                    "operations": [
                        {
                            "op": "set_profile",
                            "frame_rate_num": 30,
                            "frame_rate_den": 1,
                            "preserve_frame_numbers": True,
                        }
                    ],
                }
            )
            item = changed["project"]["tracks"][0]["items"][0]
            self.assertEqual(item["duration_frames"], 250)
            self.assertEqual(changed["project"]["markers"][0]["start_frame"], 200)

    def test_unknown_xml_is_preserved_by_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            path = Path(created["path"])
            tree = ET.parse(path)
            custom = ET.SubElement(
                tree.getroot(), "future-shotcut-element", {"mode": "keep"}
            )
            custom.text = "opaque"
            tree.write(path, encoding="utf-8", xml_declaration=True)
            current = ProjectDocument.load(path)
            edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": current.revision,
                    "validate": False,
                    "operations": [{"op": "set_notes", "notes": "preserve"}],
                }
            )
            final = ProjectDocument.load(path)
            element = final.root.find("future-shotcut-element")
            self.assertIsNotNone(element)
            assert element is not None
            self.assertEqual(element.get("mode"), "keep")
            self.assertEqual(element.text, "opaque")

    def test_legacy_timeline_is_upgraded_and_track_state_uses_hide_bits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            path = Path(created["path"])
            tree = ET.parse(path)
            xml_root = tree.getroot()
            main = xml_root.find("tractor")
            assert main is not None
            background_track = main.findall("track")[0]
            main.remove(background_track)
            background = xml_root.find("playlist[@id='background']")
            black = xml_root.find("producer[@id='black']")
            assert background is not None
            assert black is not None
            xml_root.remove(background)
            xml_root.remove(black)
            tree.write(path, encoding="utf-8", xml_declaration=True)
            legacy = ProjectDocument.load(path)
            edited = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": legacy.revision,
                    "validate": False,
                    "operations": [
                        {
                            "op": "update_track",
                            "track": "V1",
                            "hidden": True,
                            "muted": True,
                        },
                        {"op": "add_track", "kind": "video", "name": "V2"},
                    ],
                }
            )
            document = ProjectDocument.load(path)
            self.assertEqual(
                document.track_container().findall("track")[0].get("producer"),
                "background",
            )
            self.assertEqual(
                [track["name"] for track in edited["project"]["tracks"]], ["V1", "V2"]
            )
            self.assertEqual(edited["project"]["tracks"][0]["properties"]["hide"], "3")

    def test_generic_filter_can_be_added_updated_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.create_empty(root)
            added = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": created["revision"],
                    "validate": False,
                    "operations": [
                        {
                            "op": "add_filter",
                            "target": "project",
                            "service": "brightness",
                            "shotcut_filter": "brightness",
                            "properties": {"level": "0=0;30=1"},
                        }
                    ],
                }
            )
            filter_id = added["operation_results"][0]["filter_id"]
            updated = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": added["revision"],
                    "validate": False,
                    "operations": [
                        {
                            "op": "update_filter",
                            "filter_id": filter_id,
                            "enabled": False,
                            "properties": {"level": "0.5"},
                        }
                    ],
                }
            )
            document = ProjectDocument.load(Path(created["path"]))
            filter_element = document.id_map()[filter_id]
            props = {
                item.get("name"): item.text
                for item in filter_element.findall("property")
            }
            self.assertEqual(props["disable"], "1")
            self.assertEqual(props["level"], "0.5")
            removed = edit_project(
                {
                    "project_path": created["path"],
                    "expected_revision": updated["revision"],
                    "validate": False,
                    "operations": [{"op": "remove_filter", "filter_id": filter_id}],
                }
            )
            self.assertNotIn(
                filter_id, ProjectDocument.load(Path(created["path"])).id_map()
            )
            self.assertTrue(removed["edited"])


if __name__ == "__main__":
    unittest.main()
