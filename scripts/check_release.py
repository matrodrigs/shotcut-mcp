"""Validate duplicated package metadata without requiring packaging dependencies."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package_version() -> str:
    tree = ast.parse((ROOT / "shotcut_mcp" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise RuntimeError("shotcut_mcp.__version__ was not found")


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    version = package_version()
    if manifest.get("version") != version:
        raise RuntimeError(
            "manifest.json version does not match shotcut_mcp.__version__"
        )

    source = (ROOT / "shotcut_mcp" / "tools.py").read_text(encoding="utf-8")
    catalog_names = set(
        re.findall(r'^\s*"name": "([a-z][a-z0-9_]*)",$', source, re.MULTILINE)
    )
    manifest_names = {tool.get("name") for tool in manifest.get("tools", [])}
    if catalog_names != manifest_names:
        missing = sorted(catalog_names - manifest_names)
        extra = sorted(manifest_names - catalog_names)
        raise RuntimeError(
            f"manifest tool catalog mismatch; missing={missing}, extra={extra}"
        )

    package = server["packages"][0]
    server_version = server["version"]
    if f"/v{server_version}/" not in package["identifier"]:
        raise RuntimeError("server.json release URL does not match its version")
    if re.fullmatch(r"[0-9a-f]{64}", package.get("fileSha256", "")) is None:
        raise RuntimeError("server.json fileSha256 must be a lowercase SHA-256 digest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
