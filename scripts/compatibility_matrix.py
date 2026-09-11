"""Emit the Windows integration matrix from the runtime compatibility contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shotcut_mcp import TESTED_RUNTIME_STACKS  # noqa: E402

WINDOWS_ARCHIVE_SHA256 = {
    "26.6.25": "e44a28442c5686895e59eb5d4f4f86d9a71e9f5c008e0a4c35be78717ea15690",
    "26.8.1": "b0148856de01b39add4bf4d6a813bfbc554b4663b65e3ca25cb2589f47555a6a",
}


def integration_matrix() -> dict[str, list[dict[str, str]]]:
    if {
        shotcut for shotcut, _ in TESTED_RUNTIME_STACKS
    } != WINDOWS_ARCHIVE_SHA256.keys():
        raise RuntimeError("Tested Shotcut versions and pinned Windows archives differ")
    return {
        "include": [
            {"shotcut": shotcut, "mlt": mlt, "sha256": WINDOWS_ARCHIVE_SHA256[shotcut]}
            for shotcut, mlt in TESTED_RUNTIME_STACKS
        ]
    }


if __name__ == "__main__":
    print(json.dumps(integration_matrix()))
