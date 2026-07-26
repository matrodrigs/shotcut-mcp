"""Run the shared local, hook, CI, and release verification suites."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
_WINDOWS_ALIAS_DRIVES = "ZYXWVUTSRQPONMLKJIHGFED"


def _run(command: Sequence[str], *, env: dict[str, str] | None = None) -> None:
    """Run one check from the repository root without invoking a shell."""

    args = list(command)
    print(f"+ {shlex.join(args)}", flush=True)
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def _mount_windows_temp_alias(subst: str, target: Path) -> str:
    failures: list[str] = []
    for letter in _WINDOWS_ALIAS_DRIVES:
        drive = f"{letter}:"
        result = subprocess.run(
            [subst, drive, str(target)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return drive
        message = result.stderr.strip() or result.stdout.strip()
        if message:
            failures.append(f"{drive} ({message})")
    detail = "; ".join(failures[-3:]) or "no drive letter was available"
    raise RuntimeError(f"Could not mount a Windows test-path alias: {detail}")


@contextmanager
def _unit_test_environment() -> Iterator[dict[str, str] | None]:
    """Expose Windows temp files through a second path spelling during tests."""

    if os.name != "nt":
        yield None
        return

    subst = shutil.which("subst")
    if subst is None:
        raise RuntimeError("Windows path-alias tests require subst.exe")
    target = Path(tempfile.gettempdir()).resolve(strict=True)
    drive = _mount_windows_temp_alias(subst, target)
    alias = f"{drive}\\"
    environment = os.environ.copy()
    environment["TEMP"] = alias
    environment["TMP"] = alias
    print(f"Windows path-alias test root: {alias} -> {target}", flush=True)
    try:
        yield environment
    finally:
        result = subprocess.run(
            [subst, drive, "/d"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"Could not remove Windows test-path alias {drive}: {message}"
            )


def run_quality_checks() -> None:
    _run([PYTHON, "-m", "ruff", "format", "--check", "."])
    _run([PYTHON, "-m", "ruff", "check", "."])
    _run([PYTHON, "-m", "mypy"])
    _run([PYTHON, "-m", "vulture"])


def run_test_checks() -> None:
    _run(
        [
            PYTHON,
            "-B",
            "-m",
            "compileall",
            "-q",
            "shotcut_mcp",
            "scripts",
            "tests",
        ]
    )
    with _unit_test_environment() as environment:
        _run(
            [
                PYTHON,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
            env=environment,
        )
    _run([PYTHON, "-B", "scripts/check_release.py"])


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the checks shared by local development and GitHub Actions."
    )
    parser.add_argument(
        "suite",
        choices=("all", "quality", "test"),
        default="all",
        nargs="?",
        help="check group to run (default: all)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.suite in {"all", "quality"}:
            run_quality_checks()
        if args.suite in {"all", "test"}:
            run_test_checks()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"check failed: {exc}", file=sys.stderr)
        return exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
