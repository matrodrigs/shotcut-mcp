"""Canonical path and embedded-resource policy for all MCP operations."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .errors import ToolError

NETWORK_SCHEMES = frozenset(
    {
        "ftp",
        "ftps",
        "http",
        "https",
        "nfs",
        "rtmp",
        "rtp",
        "rtsp",
        "sftp",
        "smb",
        "srt",
        "tcp",
        "udp",
    }
)


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def expand_path(value: str, *, enforce_policy: bool = True) -> Path:
    """Resolve a user path and enforce the administrator's root policy."""

    if not isinstance(value, str) or not value.strip():
        raise ToolError("The path must be a non-empty string.")
    expanded = Path(os.path.expandvars(value)).expanduser()
    if (
        enforce_policy
        and _enabled("SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS")
        and not expanded.is_absolute()
    ):
        raise ToolError(
            "Relative paths are disabled by SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS."
        )
    resolved = expanded.resolve()
    configured_roots = os.environ.get("SHOTCUT_MCP_ALLOWED_ROOTS", "").strip()
    if enforce_policy and configured_roots:
        roots = [
            Path(os.path.expandvars(item)).expanduser().resolve()
            for item in configured_roots.split(os.pathsep)
            if item.strip()
        ]
        candidate = os.path.normcase(str(resolved))
        allowed = False
        for root in roots:
            try:
                allowed = os.path.commonpath(
                    [candidate, os.path.normcase(str(root))]
                ) == os.path.normcase(str(root))
            except ValueError:
                allowed = False
            if allowed:
                break
        if not allowed:
            raise ToolError(
                f"Path is outside SHOTCUT_MCP_ALLOWED_ROOTS allowed roots: {resolved}"
            )
    return resolved


def path_policy() -> dict[str, Any]:
    """Describe the effective path and resource policy without mutating it."""

    configured = os.environ.get("SHOTCUT_MCP_ALLOWED_ROOTS", "").strip()
    return {
        "allowed_roots": [item for item in configured.split(os.pathsep) if item]
        if configured
        else None,
        "require_absolute_paths": _enabled("SHOTCUT_MCP_REQUIRE_ABSOLUTE_PATHS"),
        "unsafe_consumer_properties": _enabled(
            "SHOTCUT_MCP_ALLOW_UNSAFE_CONSUMER_PROPERTIES"
        ),
        "allow_network_resources": _enabled("SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES"),
    }


def is_network_resource(value: str) -> bool:
    """Return whether an MLT resource points to a network location."""

    return (
        value.startswith(("//", "\\\\"))
        or value.partition(":")[0].lower() in NETWORK_SCHEMES
    )


def project_network_resources(project_path: Path) -> list[str]:
    """List unique network resources referenced by an MLT document."""

    try:
        root = ET.parse(project_path).getroot()
    except (ET.ParseError, OSError):
        return []
    values = [
        (element.text or "").strip()
        for element in root.findall(".//property[@name='resource']")
    ]
    values.extend(
        value.strip() for element in root.iter() if (value := element.get("resource"))
    )
    return sorted({value for value in values if is_network_resource(value)})


def enforce_project_resource_policy(project_path: Path) -> None:
    """Reject network-backed MLT resources unless the administrator opted in."""

    if _enabled("SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES"):
        return
    resources = project_network_resources(project_path)
    if resources:
        preview = ", ".join(resources[:3])
        raise ToolError(
            "Project network resources are disabled by default: "
            f"{preview}. An administrator can opt in with "
            "SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES=1."
        )
