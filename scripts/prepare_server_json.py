"""Prepare registry metadata from a versioned MCPB release artifact."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: prepare_server_json.py VERSION ARTIFACT")
    version = sys.argv[1].removeprefix("v")
    artifact = Path(sys.argv[2]).resolve(strict=True)
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version:
        raise RuntimeError(
            f"release {version} does not match manifest {manifest.get('version')}"
        )
    payload = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    payload["version"] = version
    package = payload["packages"][0]
    package["identifier"] = (
        "https://github.com/matrodrigs/shotcut-mcp/releases/download/"
        f"v{version}/{artifact.name}"
    )
    package["fileSha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (ROOT / "server.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
