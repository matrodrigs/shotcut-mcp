from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from shotcut_mcp.errors import ConflictError
from shotcut_mcp.project import (
    ProjectDocument,
    create_project,
    edit_project,
    list_backups,
)

PLUGIN_ROOT = Path(__file__).parents[1]
SERVER_PATH = PLUGIN_ROOT / "scripts" / "shotcut_mcp_server.py"


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
        self.assertEqual(len(names), 16)
        self.assertIn("edit_project", names)
        self.assertIn("shotcut_capabilities", names)
        self.assertIn("restore_project_backup", names)


class ProjectEditingTests(unittest.TestCase):
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
