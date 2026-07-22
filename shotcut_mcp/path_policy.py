"""Canonical path and embedded-resource policy for all MCP operations."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .errors import ToolError
from .mlt_xml import ResourceReference, resource_references

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


def resolve_project_resource(
    project_path: Path, xml_root: str | None, value: str
) -> Path | None:
    """Resolve one decoded local MLT resource against its project root."""

    if not value or is_network_resource(value):
        return None
    cleaned = value
    if cleaned.startswith("file://"):
        parsed = urlparse(cleaned)
        if parsed.netloc:
            cleaned = f"//{parsed.netloc}{unquote(parsed.path)}"
        else:
            cleaned = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", cleaned):
                cleaned = cleaned[1:]
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return candidate.resolve()
    base = Path(xml_root) if xml_root else project_path.parent
    if not base.is_absolute():
        base = project_path.parent / base
    return (base / candidate).resolve()


def parsed_project_resources(
    project_path: Path,
) -> tuple[ET.Element, list[ResourceReference]]:
    """Parse an MLT document and return its shared resource-reference model."""

    try:
        root = ET.parse(project_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ToolError(f"Invalid MLT XML while checking resources: {exc}") from exc
    return root, resource_references(root)


def project_network_resources(project_path: Path) -> list[str]:
    """List unique network resources referenced by an MLT document."""

    try:
        _, references = parsed_project_resources(project_path)
    except ToolError:
        return []
    return sorted(
        {
            reference.decoded_value
            for reference in references
            if is_network_resource(reference.decoded_value)
        }
    )


def enforce_project_resource_policy(project_path: Path) -> None:
    """Apply network and allowed-root policy to every embedded MLT resource."""

    try:
        root, references = parsed_project_resources(project_path)
    except ToolError:
        # Resource policy does not replace the XML/MLT validator. An unparsable
        # document has no actionable embedded paths and is rejected by that seam.
        return
    network = sorted(
        {
            reference.decoded_value
            for reference in references
            if is_network_resource(reference.decoded_value)
        }
    )
    if network and not _enabled("SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES"):
        preview = ", ".join(network[:3])
        raise ToolError(
            "Project network resources are disabled by default: "
            f"{preview}. An administrator can opt in with "
            "SHOTCUT_MCP_ALLOW_NETWORK_RESOURCES=1."
        )
    if not os.environ.get("SHOTCUT_MCP_ALLOWED_ROOTS", "").strip():
        return
    for reference in references:
        path = resolve_project_resource(
            project_path, root.get("root"), reference.decoded_value
        )
        if path is not None:
            expand_path(str(path))
