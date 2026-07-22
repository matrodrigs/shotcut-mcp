from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
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

    def test_clip_filter_clones_a_shared_producer(self) -> None:
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
                "<mlt><profile/><tractor id=\"one\"/><tractor id=\"two\"/></mlt>",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ToolError, "multiple tractors"):
                inspect_project({"path": str(project_path)})


if __name__ == "__main__":
    unittest.main()
