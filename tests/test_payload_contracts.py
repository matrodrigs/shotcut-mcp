from __future__ import annotations

import json
import math
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Union, get_args, get_origin, get_type_hints, is_typeddict
from unittest.mock import patch

from shotcut_mcp import render_jobs
from shotcut_mcp.errors import ToolError
from shotcut_mcp.media import ProbePayload, probe_media_raw, summarize_media


def matches_contract(value: object, annotation: object) -> bool:
    """Independent JSON-shape oracle for the public payload annotations under test."""
    if annotation is object:
        return True
    if annotation is type(None):
        return value is None
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (Union, types.UnionType):
        return any(matches_contract(value, item) for item in arguments)
    if is_typeddict(annotation):
        fields = get_type_hints(annotation)
        return (
            isinstance(value, dict)
            and annotation.__required_keys__ <= value.keys()
            and all(isinstance(key, str) for key in value)
            and all(
                matches_contract(value[key], item)
                for key, item in fields.items()
                if key in value
            )
        )
    if origin is list:
        return isinstance(value, list) and all(
            matches_contract(item, arguments[0]) for item in value
        )
    if origin is dict:
        return isinstance(value, dict) and all(
            matches_contract(key, arguments[0]) and matches_contract(item, arguments[1])
            for key, item in value.items()
        )
    if annotation in (int, float):
        numeric_types = (int, float) if annotation is float else (int,)
        return (
            isinstance(value, numeric_types)
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if annotation in (str, bool):
        return isinstance(value, annotation)
    raise AssertionError(f"Add explicit contract coverage for {annotation!r}")


CANDIDATES = (
    None,
    True,
    False,
    0,
    1,
    1.5,
    "value",
    [],
    {},
    {"codec": "value"},
    {"codec": 1},
    ["value"],
    [1],
    [{}],
    [{"at": 1.0, "percent": None, "frame": None}],
    [{"at": 1.0, "percent": 50.0, "frame": 2}],
    [{"at": True, "percent": 50.0, "frame": 2}],
    [{"at": 1.0, "percent": "50", "frame": 2}],
    [{"at": 1.0, "percent": 50.0, "frame": True}],
    [{"at": 1.0, "percent": 50.0}],
    float("inf"),
    float("nan"),
)


class PayloadContractTests(unittest.TestCase):
    def test_display_geometry_preserves_coded_dimensions_and_honors_rotation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "phone.mp4"
            path.touch()
            for angle, expected in (
                (90, (90, 160)),
                (-90, (90, 160)),
                (180, (160, 90)),
                (45, (None, None)),
            ):
                stream = {
                    "codec_type": "video",
                    "width": 160,
                    "height": 90,
                    "side_data_list": [
                        {"side_data_type": "Display Matrix", "rotation": angle}
                    ],
                }
                with (
                    self.subTest(angle=angle),
                    patch(
                        "shotcut_mcp.media.probe_media_raw",
                        return_value={"streams": [stream]},
                    ),
                ):
                    item = summarize_media(path)["streams"][0]
                    self.assertEqual(
                        (item["display_width"], item["display_height"]), expected
                    )
                    self.assertEqual(
                        (item["width"], item["height"], item["rotation_degrees"]),
                        (160, 90, angle),
                    )

    def test_probe_distinguishes_single_images_from_timed_streams(self) -> None:
        cases = (
            ("image2", "12/1", "0.083333", None, None),
            ("image2", "50/1", "0.040000", "2", 0.04),
            ("image2", "50/1", "0.040000", None, 0.04),
            ("gif", "50/1", "0.040000", "2", 0.04),
            ("mov,mp4", "25/1", "0.040000", "1", 0.04),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            path.touch()
            for fmt, rate, duration, count, expected in cases:
                payload = {
                    "format": {"format_name": fmt, "duration": duration},
                    "streams": [
                        {
                            "codec_type": "video",
                            "avg_frame_rate": "0/0",
                            "r_frame_rate": rate,
                            "nb_frames": count,
                        }
                    ],
                }
                with (
                    self.subTest(fmt=fmt, rate=rate, count=count),
                    patch("shotcut_mcp.media.probe_media_raw", return_value=payload),
                ):
                    summary = summarize_media(path)
                    self.assertEqual(summary["duration_seconds"], expected)
                    self.assertEqual(
                        summary["streams"][0]["frame_rate"], float(rate.split("/")[0])
                    )

    def test_render_reader_matches_declared_fields_and_requiredness(self) -> None:
        fields = get_type_hints(render_jobs.RenderJob)
        job_id = "d" * 32
        full = {
            name: next(
                value for value in CANDIDATES if matches_contract(value, annotation)
            )
            for name, annotation in fields.items()
        }
        full["job_id"] = job_id
        cases = [
            (
                f"missing {name}",
                {key: value for key, value in full.items() if key != name},
            )
            for name in fields
        ]
        cases.extend(
            (f"{name}={value!r}", {**full, name: value})
            for name in fields
            for value in CANDIDATES
        )
        sample_fields = get_type_hints(render_jobs.ProgressSample)
        sample = {
            name: next(
                value for value in CANDIDATES if matches_contract(value, annotation)
            )
            for name, annotation in sample_fields.items()
        }
        cases.extend(
            (
                f"sample {name}={value!r}",
                {**full, "progress_samples": [{**sample, name: value}]},
            )
            for name in sample_fields
            for value in CANDIDATES
        )
        cases.extend(
            (
                f"sample missing {name}",
                {
                    **full,
                    "progress_samples": [
                        {key: value for key, value in sample.items() if key != name}
                    ],
                },
            )
            for name in sample_fields
        )
        cases.append(
            ("unknown extension", {**full, "extension": {"nested": [1, "two", None]}})
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(render_jobs, "JOB_DIR", Path(directory)),
        ):
            path = render_jobs.metadata_path(job_id)
            for label, payload in cases:
                with self.subTest(case=label):
                    # Exercise the reader independently of write_job's own validation.
                    raw = json.dumps(payload).encode("utf-8")
                    path.write_bytes(raw)
                    valid = (
                        matches_contract(payload, render_jobs.RenderJob)
                        and payload.get("job_id") == job_id
                    )
                    if valid:
                        self.assertEqual(render_jobs.read_job(job_id), payload)
                    else:
                        with self.assertRaises(ToolError) as caught:
                            render_jobs.read_job(job_id)
                        self.assertEqual(
                            caught.exception.code, "invalid_render_metadata"
                        )
                    self.assertEqual(path.read_bytes(), raw)

    def test_probe_reader_matches_declared_shapes_and_preserves_extensions(
        self,
    ) -> None:
        fields = get_type_hints(ProbePayload)
        cases = [{}, {"extension": [1, "two", None]}]
        cases.extend({name: value} for name in fields for value in CANDIDATES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ffprobe = root / "ffprobe"
            ffprobe.write_bytes(b"binary")
            with patch(
                "shotcut_mcp.media.discover_executables",
                return_value=SimpleNamespace(ffprobe=ffprobe),
            ):
                for index, payload in enumerate(cases):
                    with self.subTest(payload=payload):
                        source = root / f"source-{index}.mp4"
                        source.write_bytes(b"media")
                        with patch(
                            "shotcut_mcp.media.run_capture",
                            return_value=SimpleNamespace(
                                returncode=0, stdout=json.dumps(payload), stderr=""
                            ),
                        ):
                            if matches_contract(payload, ProbePayload):
                                self.assertEqual(probe_media_raw(source), payload)
                            else:
                                with self.assertRaises(ToolError) as caught:
                                    probe_media_raw(source)
                                self.assertEqual(
                                    caught.exception.code, "media_probe_failed"
                                )
                        self.assertEqual(source.read_bytes(), b"media")
