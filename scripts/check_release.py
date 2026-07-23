"""Validate duplicated package metadata without requiring packaging dependencies."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _runtime_tool_catalog() -> dict[str, str]:
    """Load the same public tool catalog served to MCP clients."""

    sys.path.insert(0, str(ROOT))
    try:
        from shotcut_mcp.tools import TOOLS
    finally:
        del sys.path[0]
    catalog: dict[str, str] = {}
    for entry in TOOLS:
        name = entry.get("name")
        description = entry.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            raise RuntimeError("Tool names and descriptions must be strings")
        if name in catalog:
            raise RuntimeError(f"Duplicate runtime tool name: {name}")
        catalog[name] = description
    return catalog


def _manifest_tool_catalog(entries: object) -> dict[str, str]:
    """Validate and normalize the manifest's public tool declaration."""

    if not isinstance(entries, list):
        raise RuntimeError("manifest.json tools must be an array")
    catalog: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Every manifest tool must be an object")
        name = entry.get("name")
        description = entry.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            raise RuntimeError(
                "Every manifest tool requires string name and description fields"
            )
        if name in catalog:
            raise RuntimeError(f"Duplicate manifest tool name: {name}")
        catalog[name] = description
    return catalog


def _readme_tool_names(source: str) -> set[str]:
    """Read tool names from the human-facing MCP tool table."""

    marker = "## MCP tools"
    if marker not in source:
        raise RuntimeError("README.md is missing the MCP tools section")
    section = source.split(marker, 1)[1].split("\n## ", 1)[0]
    documented = re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", section, re.MULTILINE)
    if not documented:
        raise RuntimeError("README.md MCP tools table is empty")
    names = set(documented)
    if len(names) != len(documented):
        raise RuntimeError("README.md MCP tools table contains duplicate names")
    return names


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

    runtime_catalog = _runtime_tool_catalog()
    manifest_catalog = _manifest_tool_catalog(manifest.get("tools"))
    if runtime_catalog != manifest_catalog:
        runtime_names = set(runtime_catalog)
        manifest_names = set(manifest_catalog)
        missing = sorted(runtime_names - manifest_names)
        extra = sorted(manifest_names - runtime_names)
        changed = sorted(
            name
            for name in runtime_names & manifest_names
            if runtime_catalog[name] != manifest_catalog[name]
        )
        raise RuntimeError(
            "manifest tool catalog mismatch; "
            f"missing={missing}, extra={extra}, changed={changed}"
        )

    readme_names = _readme_tool_names((ROOT / "README.md").read_text(encoding="utf-8"))
    runtime_names = set(runtime_catalog)
    if runtime_names != readme_names:
        missing = sorted(runtime_names - readme_names)
        extra = sorted(readme_names - runtime_names)
        raise RuntimeError(
            f"README tool catalog mismatch; missing={missing}, extra={extra}"
        )

    site = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    count = len(runtime_catalog)
    if (
        f"<dt>{count}</dt><dd>MCP tools</dd>" not in site
        or f"See all {count} MCP tools" not in site
    ):
        raise RuntimeError("site MCP tool count does not match the runtime catalog")

    package = server["packages"][0]
    server_version = server["version"]
    if f"/v{server_version}/" not in package["identifier"]:
        raise RuntimeError("server.json release URL does not match its version")
    if re.fullmatch(r"[0-9a-f]{64}", package.get("fileSha256", "")) is None:
        raise RuntimeError("server.json fileSha256 must be a lowercase SHA-256 digest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
