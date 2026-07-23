"""Synchronize and validate release metadata without packaging dependencies."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime_tool_entries() -> list[dict[str, str]]:
    """Load the same public tool catalog served to MCP clients."""

    sys.path.insert(0, str(ROOT))
    try:
        from shotcut_mcp.tools import TOOLS
    finally:
        del sys.path[0]
    entries: list[dict[str, str]] = []
    names: set[str] = set()
    for entry in TOOLS:
        name = entry.get("name")
        description = entry.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            raise RuntimeError("Tool names and descriptions must be strings")
        if name in names:
            raise RuntimeError(f"Duplicate runtime tool name: {name}")
        names.add(name)
        entries.append({"name": name, "description": description})
    return entries


def _tool_catalog(entries: object, source: str) -> dict[str, str]:
    """Validate and normalize one public tool declaration."""

    if not isinstance(entries, list):
        raise RuntimeError(f"{source} tools must be an array")
    catalog: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError(f"Every {source} tool must be an object")
        name = entry.get("name")
        description = entry.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            raise RuntimeError(
                f"Every {source} tool requires string name and description fields"
            )
        if name in catalog:
            raise RuntimeError(f"Duplicate {source} tool name: {name}")
        catalog[name] = description
    return catalog


def _manifest_tools_bounds(source: str) -> tuple[int, int, str]:
    matches = list(
        re.finditer(
            r'(?m)^(?P<indent>[ \t]*)"tools"\s*:\s*(?P<opening>\[)',
            source,
        )
    )
    if len(matches) != 1:
        raise RuntimeError(
            "manifest.json must contain exactly one top-level tools array"
        )
    match = matches[0]
    start = match.start("opening")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(source)):
        character = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return start, index + 1, match.group("indent")
    raise RuntimeError("manifest.json tools array is not closed")


def _render_manifest_tools(source: str, entries: list[dict[str, str]]) -> str:
    manifest = json.loads(source)
    if not isinstance(manifest, dict):
        raise RuntimeError("manifest.json root must be an object")
    _tool_catalog(entries, "runtime")
    start, end, indent = _manifest_tools_bounds(source)
    newline = "\r\n" if "\r\n" in source else "\n"
    child_indent = f"{indent}  "
    rendered_entries = [
        child_indent
        + "{ "
        + f'"name": {json.dumps(entry["name"], ensure_ascii=False)}, '
        + f'"description": {json.dumps(entry["description"], ensure_ascii=False)}'
        + " }"
        for entry in entries
    ]
    rendered = (
        f"[{newline}" + f",{newline}".join(rendered_entries) + f"{newline}{indent}]"
    )
    return source[:start] + rendered + source[end:]


def _replace_site_count(source: str, count: int) -> str:
    replacements = (
        (
            r"(<dt>)\d+(</dt><dd>MCP tools</dd>)",
            rf"\g<1>{count}\g<2>",
            "summary",
        ),
        (
            r"(See all )\d+( MCP tools)",
            rf"\g<1>{count}\g<2>",
            "link",
        ),
    )
    for pattern, replacement, label in replacements:
        source, matches = re.subn(pattern, replacement, source)
        if matches != 1:
            raise RuntimeError(
                f"site must contain exactly one MCP tool count {label}; found {matches}"
            )
    return source


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


def sync_tool_contracts(root: Path, entries: list[dict[str, str]]) -> tuple[Path, ...]:
    """Write the mechanical public projections of the runtime tool catalog."""

    manifest_path = root / "manifest.json"
    site_path = root / "docs" / "index.html"
    manifest_source = manifest_path.read_text(encoding="utf-8")
    site_source = site_path.read_text(encoding="utf-8")
    sources = (
        (
            manifest_path,
            manifest_source,
            _render_manifest_tools(manifest_source, entries),
        ),
        (
            site_path,
            site_source,
            _replace_site_count(site_source, len(entries)),
        ),
    )
    changed: list[Path] = []
    for path, source, rendered in sources:
        if source == rendered:
            continue
        path.write_text(rendered, encoding="utf-8")
        changed.append(path)
    return tuple(changed)


def _catalog_difference(
    runtime_catalog: dict[str, str], documented_catalog: dict[str, str]
) -> str:
    runtime_names = set(runtime_catalog)
    documented_names = set(documented_catalog)
    missing = sorted(runtime_names - documented_names)
    extra = sorted(documented_names - runtime_names)
    changed = sorted(
        name
        for name in runtime_names & documented_names
        if runtime_catalog[name] != documented_catalog[name]
    )
    order_changed = list(runtime_catalog) != list(documented_catalog)
    return (
        f"missing={missing}, extra={extra}, changed={changed}, "
        f"order_changed={order_changed}"
    )


def validate_tool_contracts(root: Path, entries: list[dict[str, str]]) -> None:
    """Validate generated projections and the README's editorial tool coverage."""

    runtime_catalog = _tool_catalog(entries, "runtime")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("manifest.json root must be an object")
    manifest_catalog = _tool_catalog(manifest.get("tools"), "manifest")
    if entries != manifest.get("tools"):
        raise RuntimeError(
            "manifest tool catalog mismatch; "
            + _catalog_difference(runtime_catalog, manifest_catalog)
            + "; run python scripts/check_release.py --sync-tool-contracts"
        )
    if manifest.get("tools_generated") is not False:
        raise RuntimeError(
            "manifest.json tools_generated must be false for the static tool catalog"
        )

    readme_names = _readme_tool_names((root / "README.md").read_text(encoding="utf-8"))
    runtime_names = set(runtime_catalog)
    if runtime_names != readme_names:
        missing = sorted(runtime_names - readme_names)
        extra = sorted(readme_names - runtime_names)
        raise RuntimeError(
            f"README tool catalog mismatch; missing={missing}, extra={extra}"
        )

    site = (root / "docs" / "index.html").read_text(encoding="utf-8")
    if _replace_site_count(site, len(entries)) != site:
        raise RuntimeError(
            "site MCP tool count does not match the runtime catalog; "
            "run python scripts/check_release.py --sync-tool-contracts"
        )


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


def _parse_arguments(arguments: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync-tool-contracts",
        action="store_true",
        help="refresh manifest tool descriptions and website tool counts",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    options = _parse_arguments(arguments)
    runtime_entries = runtime_tool_entries()
    if options.sync_tool_contracts:
        changed = sync_tool_contracts(ROOT, runtime_entries)
        if changed:
            print(
                "Synchronized tool contracts: "
                + ", ".join(path.relative_to(ROOT).as_posix() for path in changed)
            )

    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    plugin = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    version = package_version()
    if manifest.get("version") != version:
        raise RuntimeError(
            "manifest.json version does not match shotcut_mcp.__version__"
        )
    plugin_version = plugin.get("version")
    if (
        not isinstance(plugin_version, str)
        or plugin_version.split("+", 1)[0] != version
    ):
        raise RuntimeError(
            ".codex-plugin/plugin.json base version does not match "
            "shotcut_mcp.__version__"
        )

    validate_tool_contracts(ROOT, runtime_entries)

    package = server["packages"][0]
    server_version = server["version"]
    if f"/v{server_version}/" not in package["identifier"]:
        raise RuntimeError("server.json release URL does not match its version")
    if re.fullmatch(r"[0-9a-f]{64}", package.get("fileSha256", "")) is None:
        raise RuntimeError("server.json fileSha256 must be a lowercase SHA-256 digest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
