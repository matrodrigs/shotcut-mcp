from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp.errors import ConflictError
from shotcut_mcp.project import create_project, edit_project


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


if __name__ == "__main__":
    unittest.main()
