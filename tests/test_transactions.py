from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp import project as project_module
from shotcut_mcp import project_document as project_document_module
from shotcut_mcp.errors import ConflictError, ToolError
from shotcut_mcp.project import (
    create_project,
    edit_project,
    list_backups,
    plan_project_edit,
    restore_backup,
)
from shotcut_mcp.storage import publish_new_file


class ProjectTransactionTests(unittest.TestCase):
    def test_atomic_creation_refuses_an_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.mlt"
            target = Path(directory) / "target.mlt"
            candidate.write_bytes(b"validated candidate")
            target.write_bytes(b"another writer")
            with self.assertRaises(ConflictError):
                publish_new_file(candidate, target)
            self.assertEqual(target.read_bytes(), b"another writer")
            self.assertEqual(candidate.read_bytes(), b"validated candidate")
            new_target = Path(directory) / "new.mlt"
            publish_new_file(candidate, new_target)
            self.assertEqual(new_target.read_bytes(), b"validated candidate")

    def test_authorized_creation_backs_up_a_target_that_appears_before_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.mlt"
            external = b'<mlt producer="another-writer"/>'
            real_lock = project_module.project_lock

            @contextmanager
            def concurrent_creation(target: Path) -> Iterator[None]:
                target.write_bytes(external)
                with real_lock(target):
                    yield

            with (
                patch("shotcut_mcp.project.project_lock", concurrent_creation),
                patch(
                    "shotcut_mcp.project.validate_project_file",
                    return_value={"valid": True},
                ),
            ):
                created = create_project({"project_path": str(path), "overwrite": True})
            self.assertIsNotNone(created["backup_path"])
            self.assertEqual(Path(created["backup_path"]).read_bytes(), external)

    def test_create_preserves_a_target_created_during_media_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.mlt"
            media = Path(directory) / "source.mp4"
            media.touch()
            external = b'<mlt producer="another-writer"/>'

            def probe(_path: Path) -> dict[str, object]:
                path.write_bytes(external)
                return {"format": {"duration": "10"}, "streams": []}

            with (
                patch(
                    "shotcut_mcp.project_document.probe_media_raw", side_effect=probe
                ),
                patch(
                    "shotcut_mcp.project.validate_project_file",
                    return_value={"valid": True},
                ),
                self.assertRaises(ToolError),
            ):
                create_project(
                    {
                        "project_path": str(path),
                        "overwrite": False,
                        "clips": [{"path": str(media)}],
                    }
                )
            self.assertEqual(path.read_bytes(), external)
            self.assertEqual(list_backups(path)["backup_count"], 0)

    def test_default_project_size_limit_is_128_mib(self) -> None:
        self.assertEqual(
            project_document_module.MAX_PROJECT_BYTES,
            128 * 1024 * 1024,
        )

    def test_project_size_configuration_is_bounded(self) -> None:
        cases = (
            ("invalid", 128 * 1024 * 1024),
            ("0", 1 * 1024 * 1024),
            (str(1024 * 1024 * 1024), 512 * 1024 * 1024),
        )
        for configured, expected in cases:
            with (
                self.subTest(configured=configured),
                patch.dict(
                    os.environ,
                    {"SHOTCUT_MCP_MAX_PROJECT_BYTES": configured},
                ),
            ):
                self.assertEqual(
                    project_document_module._project_size_limit(),
                    expected,
                )

    def test_edit_rejects_a_candidate_that_exceeds_the_project_size_limit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "bounded.mlt"
            with patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True},
            ):
                created = create_project({"project_path": str(project_path)})
            before = project_path.read_bytes()
            maximum = 1024 * 1024

            with (
                patch.dict(
                    os.environ,
                    {"SHOTCUT_MCP_MAX_PROJECT_BYTES": str(maximum)},
                ),
                patch(
                    "shotcut_mcp.project.validate_project_file",
                    return_value={"valid": True},
                ),
                self.assertRaises(ToolError) as raised,
            ):
                edit_project(
                    {
                        "project_path": str(project_path),
                        "expected_revision": created["revision"],
                        "operations": [{"op": "set_notes", "notes": "x" * 1_100_000}],
                    }
                )

            self.assertEqual(raised.exception.code, "project_too_large")
            self.assertEqual(raised.exception.details["maximum_bytes"], maximum)
            self.assertEqual(project_path.read_bytes(), before)
            self.assertEqual(list_backups(project_path)["backup_count"], 0)

    def test_plan_edit_returns_diff_without_changing_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            with patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True},
            ):
                created = create_project({"project_path": str(project_path)})
                before = project_path.read_bytes()

                plan = plan_project_edit(
                    {
                        "project_path": str(project_path),
                        "expected_revision": created["revision"],
                        "operations": [
                            {"op": "add_track", "kind": "audio", "name": "Voice"}
                        ],
                    }
                )

            self.assertTrue(plan["changed"])
            self.assertIn("Voice", plan["unified_diff"])
            self.assertNotEqual(plan["prospective_revision"], created["revision"])
            self.assertEqual(project_path.read_bytes(), before)
            self.assertEqual(list_backups(project_path)["backup_count"], 0)

    def test_edit_aborts_if_project_changes_while_candidate_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"

            with patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True},
            ):
                created = create_project({"project_path": str(project_path)})

            external_contents = b'<mlt producer="external-editor"/>\n'

            def validate_after_external_save(
                _candidate_path: str, **_kwargs: object
            ) -> dict[str, bool]:
                project_path.write_bytes(external_contents)
                return {"valid": True}

            with (
                patch(
                    "shotcut_mcp.project.validate_project_file",
                    side_effect=validate_after_external_save,
                ),
                self.assertRaises(ConflictError),
            ):
                edit_project(
                    {
                        "project_path": str(project_path),
                        "operations": [
                            {"op": "add_track", "kind": "video", "name": "V2"}
                        ],
                        "expected_revision": created["revision"],
                    }
                )

            self.assertEqual(project_path.read_bytes(), external_contents)

    def test_backups_are_isolated_between_similarly_named_projects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "movie.mlt"
            second_path = Path(directory) / "movie.cut.mlt"

            with patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True},
            ):
                first = create_project({"project_path": str(first_path)})
                second = create_project({"project_path": str(second_path)})
                first_edit = edit_project(
                    {
                        "project_path": str(first_path),
                        "operations": [{"op": "add_track", "kind": "video"}],
                        "expected_revision": first["revision"],
                    }
                )
                edit_project(
                    {
                        "project_path": str(second_path),
                        "operations": [{"op": "add_track", "kind": "video"}],
                        "expected_revision": second["revision"],
                    }
                )

                first_backups = list_backups(first_path)
                second_backups = list_backups(second_path)
                self.assertEqual(first_backups["backup_count"], 1)
                self.assertEqual(second_backups["backup_count"], 1)

                with self.assertRaises(ToolError):
                    restore_backup(
                        {
                            "project_path": str(first_path),
                            "backup_path": second_backups["backups"][0]["path"],
                            "expected_revision": first_edit["revision"],
                        }
                    )

    def test_restore_rejects_an_unrecognized_file_in_the_backup_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "movie.mlt"
            with patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True},
            ):
                created = create_project({"project_path": str(project_path)})
                edited = edit_project(
                    {
                        "project_path": str(project_path),
                        "operations": [{"op": "add_track", "kind": "audio"}],
                        "expected_revision": created["revision"],
                    }
                )
                backup = Path(list_backups(project_path)["backups"][0]["path"])
                rogue = backup.parent / "injected.mlt"
                rogue.write_bytes(backup.read_bytes())

                with self.assertRaisesRegex(
                    ToolError, "not one of this project's backups"
                ):
                    restore_backup(
                        {
                            "project_path": str(project_path),
                            "backup_path": str(rogue),
                            "expected_revision": edited["revision"],
                        }
                    )


if __name__ == "__main__":
    unittest.main()
