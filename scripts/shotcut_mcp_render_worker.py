#!/usr/bin/env python3
"""Bootstrap a durable render worker from any client working directory."""

from __future__ import annotations

import os
import sys
import traceback
from contextlib import suppress
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    # Keep diagnostics available even when importing the worker itself fails.
    diagnostic_path = sys.argv.pop()
    try:
        from shotcut_mcp.render_worker import main as worker_main

        return worker_main()
    except Exception:
        diagnostic = traceback.format_exc().encode("utf-8", errors="replace")[-16384:]
        with suppress(OSError):
            descriptor = os.open(
                diagnostic_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(diagnostic)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
