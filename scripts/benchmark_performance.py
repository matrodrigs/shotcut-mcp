"""Measure public MCP calls against temporary synthetic timelines and real MLT."""

from __future__ import annotations

import argparse
import copy
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shotcut_mcp import __version__  # noqa: E402
from shotcut_mcp.platform import discover_executables, run_capture  # noqa: E402
from shotcut_mcp.project import ProjectDocument  # noqa: E402
from shotcut_mcp.server import ProtocolSession, handle_request  # noqa: E402


def measure(operation: Callable[[], object], samples: int) -> dict[str, object]:
    durations = []
    for _ in range(samples):
        started = time.perf_counter()
        operation()
        durations.append(round((time.perf_counter() - started) * 1000, 3))
    return {
        "first_ms": durations[0],
        "median_ms": statistics.median(durations),
        "repeat_median_ms": statistics.median(durations[1:]) if samples > 1 else None,
        "samples_ms": durations,
    }


def benchmark(
    root: Path, clips: int, samples: int, uhd: bool, diff_lines: int
) -> dict[str, object]:
    executables = discover_executables()
    if executables.ffmpeg is None:
        raise RuntimeError("Install Shotcut or configure SHOTCUT_FFMPEG_PATH.")
    width, height = (3840, 2160) if uhd else (1920, 1080)
    media = root / "source.mp4"
    generated = run_capture(
        [
            str(executables.ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate=30:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "15",
            str(media),
        ],
        timeout=120,
    )
    if generated.returncode:
        raise RuntimeError(generated.stderr)
    path = root / "timeline.mlt"
    document = ProjectDocument.new(
        path, width=width, height=height, fps_num=30, fps_den=1, title="Benchmark"
    )
    document.add_clip(
        {"path": str(media), "track": "V1", "in_frame": 0, "out_frame": 29}
    )
    playlist = document.tracks()[0].playlist
    entry = playlist.find("entry")
    assert entry is not None
    for _ in range(clips - 1):
        playlist.append(copy.deepcopy(entry))
    document.update_main_duration()
    path.write_bytes(document.to_bytes())
    revision = ProjectDocument.load(path).revision
    session = ProtocolSession(protocol_version="2025-11-25")

    def call(name: str, arguments: dict[str, object]) -> object:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            session,
        )
        if response is None or "error" in response:
            raise RuntimeError(response)
        result = response["result"]
        if not isinstance(result, dict) or result.get("isError"):
            raise RuntimeError(result)
        return result

    operations: dict[str, Callable[[], object]] = {
        "status": lambda: call("shotcut_status", {}),
        "doctor": lambda: call("shotcut_doctor", {}),
        "inspect": lambda: call("inspect_project", {"path": str(path)}),
        "plan_notes": lambda: call(
            "plan_project_edit",
            {
                "project_path": str(path),
                "expected_revision": revision,
                "operations": [{"op": "set_notes", "notes": "Planned benchmark"}],
                "max_diff_lines": 20,
            },
        ),
        "preview": lambda: call(
            "render_preview",
            {
                "project_path": str(path),
                "frame": 15,
                "output_path": str(root / "preview.png"),
                "overwrite": True,
            },
        ),
        "contact_sheet_12": lambda: call(
            "render_contact_sheet",
            {
                "project_path": str(path),
                "sample_count": 12,
                "output_path": str(root / "sheet.png"),
                "overwrite": True,
            },
        ),
    }
    measurements = {}
    for name, operation in operations.items():
        measurements[name] = measure(operation, samples)
        print(f"{name}: {measurements[name]}", file=sys.stderr, flush=True)
    if diff_lines:
        diff_path = root / "large-notes.mlt"
        diff_document = ProjectDocument.new(
            diff_path,
            width=320,
            height=240,
            fps_num=30,
            fps_den=1,
            title="\n".join(
                f"Original note {index}: {'a' * 80}" for index in range(diff_lines)
            ),
        )
        diff_path.write_bytes(diff_document.to_bytes())
        diff_arguments: dict[str, object] = {
            "project_path": str(diff_path),
            "expected_revision": ProjectDocument.load(diff_path).revision,
            "operations": [
                {
                    "op": "set_notes",
                    "notes": "\n".join(
                        f"Revised note {index}: {'b' * 80}"
                        for index in range(diff_lines)
                    ),
                }
            ],
            "max_diff_lines": 20,
        }
        tracemalloc.start()
        try:
            measurements["plan_large_diff"] = measure(
                lambda: call("plan_project_edit", diff_arguments), 1
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        measurements["plan_large_diff"]["peak_python_bytes"] = peak
        print(
            f"plan_large_diff: {measurements['plan_large_diff']}",
            file=sys.stderr,
            flush=True,
        )
    return {
        "version": __version__,
        "python": platform.python_version(),
        "system": platform.platform(),
        "clips": clips,
        "width": width,
        "height": height,
        "project_bytes": path.stat().st_size,
        "diff_input_lines": diff_lines,
        "scope": "Public MCP dispatch; excludes client transport and AI latency. Synthetic repeated one-second source; no effects.",
        "measurements": measurements,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clips", type=int, default=1000)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--4k", dest="uhd", action="store_true")
    parser.add_argument("--diff-lines", type=int, default=10000)
    arguments = parser.parse_args()
    if not 1 <= arguments.clips <= 10000 or not 1 <= arguments.samples <= 20:
        parser.error("Use 1..10000 clips and 1..20 samples.")
    if not 0 <= arguments.diff_lines <= 100000:
        parser.error("Use 0..100000 diff lines; zero skips the memory measurement.")
    with tempfile.TemporaryDirectory(prefix="shotcut-mcp-benchmark-") as directory:
        result = benchmark(
            Path(directory),
            arguments.clips,
            arguments.samples,
            arguments.uhd,
            arguments.diff_lines,
        )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
