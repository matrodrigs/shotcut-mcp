#!/usr/bin/env python3
"""Plugin entry point for the Shotcut MCP stdio server."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from shotcut_mcp.errors import ToolError  # noqa: E402
from shotcut_mcp.platform import compatibility_doctor, status  # noqa: E402
from shotcut_mcp.server import main as serve_main  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="print Python and Shotcut readiness as JSON, then exit without starting MCP",
    )
    args = parser.parse_args(argv)
    if not args.check:
        return serve_main()

    report: dict[str, object] = {
        "python": {"executable": sys.executable, "version": sys.version.split()[0]},
        "runtime_ready": False,
    }
    try:
        report["runtime"] = status()
        doctor = compatibility_doctor()
        report["doctor"] = doctor
        report["runtime_ready"] = doctor["runtime_ready"]
    except ToolError as exc:
        report["error"] = str(exc)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["runtime_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
