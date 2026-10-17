from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp.errors import ConflictError
from shotcut_mcp.project import (
    create_project,
    edit_project,
    inspect_project,
    list_backups,
)


class ProjectLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project.mlt"
        validation = patch(
            "shotcut_mcp.project.validate_project_file", return_value={"valid": True}
        )
        validation.start()
        self.addCleanup(validation.stop)
        created = create_project({"project_path": str(self.project)})
        self.arguments = {
            "project_path": str(self.project),
            "expected_revision": created["revision"],
            "operations": [{"op": "set_notes", "notes": "first writer"}],
        }
        self.original = self.project.read_bytes()
        self.legacy_lock = self.project.with_suffix(".mlt.shotcut-mcp.lock")

    def stale_lock(self, pid: int) -> None:
        self.legacy_lock.write_text(json.dumps({"pid": pid, "created_at": 0}))
        old = time.time() - 900
        os.utime(self.legacy_lock, (old, old))

    def test_stale_lock_reclamation_excludes_a_second_project_writer(self) -> None:
        self.stale_lock(2**30)
        reclaiming = threading.Event()
        proceed = threading.Event()
        results: list[dict[str, object]] = []
        failures: list[Exception] = []

        def alive(_pid: int) -> bool:
            if threading.current_thread().name == "reclaiming-writer":
                reclaiming.set()
                if not proceed.wait(timeout=5):
                    raise RuntimeError("Stale-lock test timed out.")
            return False

        def edit() -> None:
            try:
                results.append(edit_project(self.arguments))
            except Exception as exc:
                failures.append(exc)

        with patch("shotcut_mcp.storage.process_is_alive", side_effect=alive):
            writer = threading.Thread(target=edit, name="reclaiming-writer")
            writer.start()
            try:
                self.assertTrue(reclaiming.wait(timeout=5))
                with self.assertRaises(ConflictError) as caught:
                    edit_project(
                        {
                            **self.arguments,
                            "operations": [
                                {"op": "set_notes", "notes": "second writer"}
                            ],
                        }
                    )
                self.assertEqual(caught.exception.code, "project_locked")
                self.assertEqual(self.project.read_bytes(), self.original)
            finally:
                proceed.set()
                writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["edited"])
        self.assertEqual(
            inspect_project({"path": str(self.project)})["notes"], "first writer"
        )
        backups = list_backups(self.project)["backups"]
        self.assertEqual(len(backups), 1)
        self.assertEqual(Path(backups[0]["path"]).read_bytes(), self.original)
        self.assertFalse(self.legacy_lock.exists())

    def test_live_legacy_owner_still_blocks_an_upgraded_writer(self) -> None:
        self.stale_lock(os.getpid())
        with self.assertRaises(ConflictError) as caught:
            edit_project(self.arguments)
        self.assertEqual(caught.exception.code, "project_locked")
        self.assertEqual(self.project.read_bytes(), self.original)
        self.assertTrue(self.legacy_lock.exists())


if __name__ == "__main__":
    unittest.main()
