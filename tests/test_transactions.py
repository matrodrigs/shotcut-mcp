from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp.errors import ConflictError, ToolError
from shotcut_mcp.project import create_project, edit_project, list_backups, restore_backup


class ProjectTransactionTests(unittest.TestCase):
    def test_edit_aborts_if_project_changes_while_candidate_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"

            with patch("shotcut_mcp.project.validate_project_file", return_value={"valid": True}):
                created = create_project({"project_path": str(project_path)})

            external_contents = b"<mlt producer=\"external-editor\"/>\n"

            def validate_after_external_save(
                _candidate_path: str, **_kwargs: object
            ) -> dict[str, bool]:
                project_path.write_bytes(external_contents)
                return {"valid": True}

            with patch(
                "shotcut_mcp.project.validate_project_file",
                side_effect=validate_after_external_save,
            ):
                with self.assertRaises(ConflictError):
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

            with patch("shotcut_mcp.project.validate_project_file", return_value={"valid": True}):
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


if __name__ == "__main__":
    unittest.main()
