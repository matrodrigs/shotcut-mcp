"""Require a successful main-branch CI run before publishing a tagged commit."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from typing import Any


def _list_ci_runs(repository: str, commit_sha: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repository,
            "--workflow",
            "CI",
            "--commit",
            commit_sha,
            "--event",
            "push",
            "--limit",
            "20",
            "--json",
            "databaseId,headBranch,status,conclusion,url,createdAt",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise RuntimeError(f"Could not inspect CI runs: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub CLI returned invalid CI run data.") from exc
    if not isinstance(payload, list):
        raise RuntimeError("GitHub CLI returned an invalid CI run collection.")
    return [item for item in payload if isinstance(item, dict)]


def _latest_main_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [run for run in runs if run.get("headBranch") == "main"]
    if not candidates:
        return None
    return max(candidates, key=lambda run: str(run.get("createdAt", "")))


def require_green_ci(
    repository: str,
    commit_sha: str,
    *,
    timeout_seconds: float = 600,
    poll_seconds: float = 10,
) -> dict[str, Any]:
    """Wait for the tagged SHA's main push CI and require a successful result."""

    if not repository.strip() or not commit_sha.strip():
        raise ValueError("repository and commit_sha must be non-empty")
    if timeout_seconds < 0 or poll_seconds <= 0:
        raise ValueError(
            "timeout_seconds must be non-negative and poll_seconds positive"
        )

    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] | None = None
    while True:
        latest = _latest_main_run(_list_ci_runs(repository, commit_sha))
        if latest is not None and latest.get("status") == "completed":
            conclusion = latest.get("conclusion")
            url = latest.get("url") or "unknown run URL"
            if conclusion != "success":
                raise RuntimeError(
                    f"CI for tagged commit {commit_sha} concluded {conclusion}: {url}"
                )
            return latest

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            state = latest.get("status") if latest is not None else "not found"
            raise RuntimeError(
                f"CI for tagged commit {commit_sha} did not complete successfully "
                f"within {timeout_seconds:g} seconds; latest state: {state}."
            )
        time.sleep(min(poll_seconds, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="GitHub repository in owner/name form")
    parser.add_argument("commit_sha", help="Full commit SHA referenced by the tag")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--poll", type=float, default=10)
    arguments = parser.parse_args()
    try:
        run = require_green_ci(
            arguments.repository,
            arguments.commit_sha,
            timeout_seconds=arguments.timeout,
            poll_seconds=arguments.poll,
        )
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Verified successful CI run: {run.get('url', 'unknown run URL')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
