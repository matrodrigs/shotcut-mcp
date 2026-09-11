from __future__ import annotations

import ast
import re
import unittest
from graphlib import TopologicalSorter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_MODULES = {path.stem for path in (ROOT / "shotcut_mcp").glob("*.py")}


def runtime_dependencies(source: str) -> set[str]:
    dependencies: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and ast.unparse(node.test) in {
            "TYPE_CHECKING",
            "typing.TYPE_CHECKING",
        }:
            for statement in node.orelse:
                visit(statement)
            return
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (node.level and not module) or module == "shotcut_mcp":
                dependencies.update(
                    alias.name for alias in node.names if alias.name in PACKAGE_MODULES
                )
            elif node.level:
                dependencies.add(module.split(".")[0])
            elif module.startswith("shotcut_mcp."):
                dependencies.add(module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("shotcut_mcp."):
                    dependencies.add(alias.name.split(".")[1])
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return dependencies - {"errors", "__init__"}


class ArchitectureTests(unittest.TestCase):
    def test_runtime_imports_match_the_documented_dependency_graph(self) -> None:
        paths = sorted((ROOT / "shotcut_mcp").glob("*.py"))
        actual = {
            path.stem: runtime_dependencies(path.read_text(encoding="utf-8"))
            for path in paths
        }
        architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
        rows = re.findall(r"^([a-z_]+) -> ([a-z_, ]+)$", architecture, re.MULTILINE)
        self.assertEqual(len(rows), len({name for name, _ in rows}))
        documented = {name: set(targets.split(", ")) for name, targets in rows}
        self.assertLessEqual(documented.keys(), actual.keys())
        for name, dependencies in actual.items():
            with self.subTest(module=name):
                self.assertEqual(dependencies, documented.get(name, set()))

    def test_runtime_imports_are_acyclic(self) -> None:
        graph = {
            path.stem: runtime_dependencies(path.read_text(encoding="utf-8"))
            for path in (ROOT / "shotcut_mcp").glob("*.py")
        }
        tuple(TopologicalSorter(graph).static_order())

    def test_dependency_scan_separates_runtime_and_type_only_imports(self) -> None:
        source = """
from typing import TYPE_CHECKING
import typing
from . import SHOTCUT_VERSION
from . import render_jobs
from shotcut_mcp import MLT_VERSION
from .errors import ToolError
from .storage import OutputTransaction
from shotcut_mcp.media import probe_media_raw
import shotcut_mcp.protocol
from shotcut_mcp import render
if TYPE_CHECKING:
    from .project_snapshot import ProjectSnapshot
else:
    from .path_policy import expand_path
if typing.TYPE_CHECKING:
    import shotcut_mcp.project_document
def inspect():
    from .platform import status
"""
        self.assertEqual(
            runtime_dependencies(source),
            {
                "storage",
                "media",
                "protocol",
                "render",
                "render_jobs",
                "path_policy",
                "platform",
            },
        )
