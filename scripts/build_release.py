"""Build and verify a deterministic MCPB release bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
STATIC_MEMBERS = (
    Path("LICENSE"),
    Path("README.md"),
    Path("manifest.json"),
    Path("scripts/shotcut_mcp_server.py"),
)
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def package_members() -> tuple[Path, ...]:
    """Return the complete, ordered runtime file set for the MCPB."""

    package_files = tuple(sorted((ROOT / "shotcut_mcp").rglob("*.py")))
    relative_package_files = tuple(path.relative_to(ROOT) for path in package_files)
    return tuple(sorted((*STATIC_MEMBERS, *relative_package_files)))


def release_notes(version: str) -> str:
    """Extract the closed changelog section for *version*."""

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(
        rf"^## {re.escape(version)} \(([^)]+)\)\s*$", changelog, re.MULTILINE
    )
    if heading is None:
        raise RuntimeError(f"CHANGELOG.md has no release section for {version}")
    if heading.group(1).strip().casefold() == "unreleased":
        raise RuntimeError(f"CHANGELOG.md release {version} is still unreleased")
    next_heading = re.search(r"^## ", changelog[heading.end() :], re.MULTILINE)
    end = heading.end() + next_heading.start() if next_heading else len(changelog)
    notes = changelog[heading.end() : end].strip()
    if not notes:
        raise RuntimeError(f"CHANGELOG.md release {version} has no notes")
    return notes + "\n"


def _zip_info(path: Path) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path.as_posix(), FIXED_ZIP_TIME)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def verify_bundle(artifact: Path, version: str) -> tuple[str, ...]:
    """Verify that *artifact* contains exactly the supported runtime payload."""

    expected = tuple(sorted(path.as_posix() for path in package_members()))
    with zipfile.ZipFile(artifact) as bundle:
        names = tuple(sorted(bundle.namelist()))
        if names != expected:
            missing = sorted(set(expected) - set(names))
            extra = sorted(set(names) - set(expected))
            raise RuntimeError(
                f"MCPB payload mismatch; missing={missing}, extra={extra}"
            )
        manifest = json.loads(bundle.read("manifest.json"))
        if manifest.get("version") != version:
            raise RuntimeError(
                f"MCPB manifest version {manifest.get('version')} "
                f"does not match {version}"
            )
        entry_point = manifest.get("server", {}).get("entry_point")
        if entry_point not in names:
            raise RuntimeError(f"MCPB entry point is missing: {entry_point}")
        bad_member = bundle.testzip()
        if bad_member is not None:
            raise RuntimeError(f"MCPB contains a corrupt member: {bad_member}")
    return names


def build_release(version: str, output_dir: Path) -> dict[str, Any]:
    """Create the versioned MCPB, checksum and release notes."""

    if VERSION_PATTERN.fullmatch(version) is None:
        raise RuntimeError(f"release version must be X.Y.Z, received {version!r}")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version:
        raise RuntimeError(
            f"release {version} does not match manifest {manifest.get('version')}"
        )
    notes = release_notes(version)
    members = package_members()
    for relative in members:
        if not (ROOT / relative).is_file():
            raise RuntimeError(f"release member is missing: {relative.as_posix()}")

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"shotcut-mcp-{version}.mcpb"
    with zipfile.ZipFile(
        artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as bundle:
        for relative in members:
            bundle.writestr(_zip_info(relative), (ROOT / relative).read_bytes())

    names = verify_bundle(artifact, version)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksum = artifact.with_suffix(artifact.suffix + ".sha256")
    checksum.write_text(f"{digest}  {artifact.name}\n", encoding="ascii")
    notes_path = output_dir / f"release-notes-{version}.md"
    notes_path.write_text(notes, encoding="utf-8")
    return {
        "artifact": str(artifact),
        "checksum": str(checksum),
        "digest": digest,
        "entries": len(names),
        "notes": str(notes_path),
        "version": version,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version without the v prefix")
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "dist", help="artifact directory"
    )
    arguments = parser.parse_args()
    print(
        json.dumps(
            build_release(arguments.version.removeprefix("v"), arguments.output_dir),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
