from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp.errors import ToolError
from shotcut_mcp.project import ProjectDocument, create_project, edit_project
from shotcut_mcp.tools import inspect_project


class ProjectModelTests(unittest.TestCase):
    def setUp(self) -> None:
        validation = patch(
            "shotcut_mcp.project.validate_project_file", return_value={"valid": True}
        )
        validation.start()
        self.addCleanup(validation.stop)

    def test_missing_project_reports_actionable_error_metadata(self) -> None:
        missing = Path("C:/definitely-missing/shotcut-project.mlt")
        with self.assertRaisesRegex(ToolError, "Project not found") as caught:
            ProjectDocument.load(missing)

        self.assertEqual(caught.exception.code, "project_not_found")
        self.assertEqual(
            caught.exception.recommended_action, "check_project_path_and_retry"
        )
        self.assertEqual(caught.exception.details["path"], str(missing))

    def test_edit_operation_errors_include_index_and_recovery_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            created = create_project({"project_path": str(project_path)})
            with self.assertRaisesRegex(ToolError, "Operation 0 failed") as caught:
                edit_project(
                    {
                        "project_path": str(project_path),
                        "expected_revision": created["revision"],
                        "operations": [{"op": "set_notes", "notes": 42}],
                    }
                )

        self.assertEqual(caught.exception.code, "edit_operation_rejected")
        self.assertEqual(caught.exception.recommended_tool, "shotcut_capabilities")
        self.assertEqual(caught.exception.details["operation_index"], 0)
        self.assertEqual(caught.exception.details["operation"], "set_notes")

    def test_clip_filter_clones_a_shared_producer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            created = create_project({"project_path": str(project_path)})
            edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 10,
                        },
                    ],
                }
            )
            document = ProjectDocument.load(project_path)
            playlist = document.tracks()[0].playlist
            playlist.append(copy.deepcopy(document.sequence(playlist)[0]))
            shared_source = document.to_bytes()
            project_path.write_bytes(shared_source)
            shared_revision = hashlib.sha256(shared_source).hexdigest()

            filtered = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": shared_revision,
                    "operations": [
                        {
                            "op": "add_filter",
                            "target": "clip",
                            "track": "V1",
                            "item_index": 0,
                            "service": "brightness",
                        }
                    ],
                }
            )

            first, second = filtered["project"]["tracks"][0]["items"]
            self.assertNotEqual(first["producer_id"], second["producer_id"])
            self.assertEqual(len(first["filters"]), 1)
            self.assertEqual(second["filters"], [])

    def test_snapshot_distinguishes_project_filters_from_media_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            created = create_project({"project_path": str(project_path)})
            edited = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_filter",
                            "target": "project",
                            "service": "brightness",
                        }
                    ],
                }
            )

            snapshot = edited["project"]
            self.assertEqual(snapshot["resources"], [])
            self.assertEqual(snapshot["missing_resources"], [])
            self.assertEqual(snapshot["filters"][0]["service"], "brightness")

    def test_removing_the_last_clip_removes_its_generated_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            created = create_project({"project_path": str(project_path)})
            generated = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 10,
                        }
                    ],
                }
            )
            producer_id = generated["operation_results"][0]["producer_id"]

            removed = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": generated["revision"],
                    "operations": [
                        {
                            "op": "remove_item",
                            "track": "V1",
                            "item_index": 0,
                            "ripple": True,
                        }
                    ],
                }
            )

            self.assertNotIn(
                producer_id, ProjectDocument.load(Path(removed["path"])).id_map()
            )

    def test_duplicate_xml_ids_are_rejected_instead_of_silently_shadowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            create_project({"project_path": str(project_path)})
            document = ProjectDocument.load(project_path)
            transition = document.root.find(".//transition")
            self.assertIsNotNone(transition)
            transition.set("id", "black")
            project_path.write_bytes(document.to_bytes())

            with self.assertRaisesRegex(ToolError, "Duplicate XML id"):
                inspect_project({"path": str(project_path)})

    def test_ambiguous_main_tractors_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "ambiguous.mlt"
            project_path.write_text(
                '<mlt><profile/><tractor id="one"/><tractor id="two"/></mlt>',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ToolError, "multiple tractors"):
                inspect_project({"path": str(project_path)})

    def test_edit_preserves_root_content_when_ordering_timeline_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ordering.mlt"
            path.write_text(
                """<mlt producer="main">
                  <profile width="1920" height="1080" frame_rate_num="30" frame_rate_den="1"/>
                  <producer id="early"><property name="mlt_service">color</property></producer>
                  <playlist id="background"><entry producer="black" in="0" out="29"/></playlist>
                  <playlist id="video">
                    <property name="shotcut:video">1</property>
                    <entry producer="late" in="0" out="29"/>
                  </playlist>
                  <!--keep this comment-->
                  <chain id="late" out="29">
                    <property name="mlt_service">color</property>
                    <property name="custom">keep this property</property>
                  </chain>
                  <extension id="unknown"><nested value="preserved"/></extension>
                  <producer id="black" out="29"><property name="length">30</property></producer>
                  <tractor id="nested"><track producer="early"/></tractor>
                  <tractor id="main" in="0" out="29">
                    <property name="shotcut">1</property>
                    <track producer="background"/>
                    <track producer="video"/>
                  </tractor>
                  <producer id="last"><property name="mlt_service">color</property></producer>
                </mlt>""",
                encoding="utf-8",
            )
            before = ProjectDocument.load(path)
            before.to_bytes()
            preserved = {
                child.get("id"): ET.tostring(child).rstrip()
                for child in before.root
                if child.get("id") not in {None, "main"}
            }
            for notes in ("reordered", "already ordered"):
                with self.subTest(notes=notes):
                    current = ProjectDocument.load(path)
                    result = edit_project(
                        {
                            "project_path": str(path),
                            "expected_revision": current.revision,
                            "operations": [{"op": "set_notes", "notes": notes}],
                        }
                    )
                    updated = ProjectDocument.load(path)
                    self.assertEqual(
                        [child.get("id") or child.tag for child in updated.root],
                        [
                            "profile",
                            "early",
                            "background",
                            "late",
                            "nested",
                            "last",
                            "video",
                            ET.Comment,
                            "unknown",
                            "black",
                            "main",
                        ],
                    )
                    for identifier, serialized in preserved.items():
                        self.assertEqual(
                            ET.tostring(updated.id_map()[identifier]).rstrip(),
                            serialized,
                        )
                    self.assertEqual(
                        next(
                            child.text
                            for child in updated.root
                            if child.tag is ET.Comment
                        ),
                        "keep this comment",
                    )
                    self.assertEqual(result["project"]["notes"], notes)
                    self.assertEqual(result["project"]["duration_frames"], 30)
                    self.assertEqual(
                        result["project"]["tracks"][0]["items"][0]["producer_id"],
                        "late",
                    )


if __name__ == "__main__":
    unittest.main()
