from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp.errors import RequestCancelled
from shotcut_mcp.processes import creation_flags, process_is_alive, run_capture
from shotcut_mcp.protocol import request_cancellation


class ProcessObservationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process tree")
    def test_cancellation_stops_the_command_and_its_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "pids.json"
            script = """import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
ready = pathlib.Path(sys.argv[1])
pending = ready.with_suffix(".tmp")
pending.write_text(json.dumps([os.getpid(), child.pid]))
pending.rename(ready)
child.wait()
"""
            event = threading.Event()

            def cancel_when_ready() -> None:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                event.set()

            watcher = threading.Thread(target=cancel_when_ready)
            watcher.start()
            try:
                with request_cancellation(event), self.assertRaises(RequestCancelled):
                    run_capture([sys.executable, "-c", script, str(ready)], timeout=8)
                for pid in json.loads(ready.read_text()):
                    self.assertFalse(
                        process_is_alive(pid), f"Process {pid} outlived cancellation"
                    )
            finally:
                watcher.join(timeout=6)

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
