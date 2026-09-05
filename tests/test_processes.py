from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from shotcut_mcp.processes import creation_flags, process_is_alive


class ProcessObservationTests(unittest.TestCase):
    def test_liveness_checks_do_not_signal_or_terminate_a_running_process(self) -> None:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; print('ready', flush=True); print(sys.stdin.readline().strip(), flush=True)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creation_flags(),
            start_new_session=os.name != "nt",
        ) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                for _ in range(3):
                    self.assertTrue(process_is_alive(process.pid))
                    self.assertIsNone(process.poll())
                stdout, stderr = process.communicate("still alive\n", timeout=5)
                self.assertEqual(stdout.strip(), "still alive", stderr)
                self.assertEqual(process.returncode, 0)
                self.assertFalse(process_is_alive(process.pid))
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    @unittest.skipIf(os.name == "nt", "POSIX permission semantics")
    def test_access_denied_does_not_prove_a_process_is_dead(self) -> None:
        with patch("shotcut_mcp.processes.os.kill", side_effect=PermissionError):
            self.assertTrue(process_is_alive(123))

    def test_nonpositive_pids_are_not_processes(self) -> None:
        self.assertFalse(process_is_alive(0))
        self.assertFalse(process_is_alive(-1))
