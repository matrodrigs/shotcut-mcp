from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import platform, render_jobs
from shotcut_mcp import project as project_module
from shotcut_mcp import render as render_module
from shotcut_mcp.errors import ToolError
from shotcut_mcp.path_policy import project_network_resources
from shotcut_mcp.project import (
    ProjectDocument,
    create_project,
    diagnose_missing_media,
    edit_project,
    plan_project_edit,
)
from shotcut_mcp.tools import render_contact_sheet_tool


class BacklogProjectFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        validation = patch(
            "shotcut_mcp.project.validate_project_file", return_value={"valid": True}
        )
        validation.start()
        self.addCleanup(validation.stop)

    @staticmethod
    def _media_patch() -> object:
        return patch(
            "shotcut_mcp.project_document.probe_media_raw",
            return_value={"format": {"duration": "10"}, "streams": []},
        )

    def test_new_projects_use_canonical_processing_mode_and_semantic_hdr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "color.mlt"
            created = create_project({"project_path": str(path)})
            self.assertEqual(
                created["project"]["color_workflow"]["processing_mode"],
                "Native8Cpu",
            )
            changed = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "set_color_workflow",
                            "workflow": "hlg",
                            "processing_mode": "Native10Cpu",
                        }
                    ],
                }
            )
            self.assertEqual(
                changed["project"]["color_workflow"],
                {
                    "processing_mode": "Native10Cpu",
                    "color_transfer": "arib-std-b67",
                    "colorspace": "2020",
                    "dynamic_range": "hlg",
                },
            )

    def test_validate_project_combines_mlt_resources_and_required_services(
        self,
    ) -> None:
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "shotcut-26.6"
            / "multitrack-ripple.mlt"
        )
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            shutil.copy2(fixture, project_path)

            def services(kind: str) -> dict[str, object]:
                available = {
                    "producer": ["color", "avformat-novalidate"],
                    "filter": [],
                    "transition": [],
                    "consumer": [],
                    "link": [],
                }
                return {
                    "kind": kind,
                    "count": len(available[kind]),
                    "services": available[kind],
                }

            with patch(
                "shotcut_mcp.project.list_services", side_effect=services, create=True
            ):
                result = project_module.validate_project({"path": str(project_path)})

        self.assertTrue(result["valid"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["checks"]["resources"]["status"], "failed")
        self.assertEqual(
            result["checks"]["resources"]["missing_resources"],
            [str(project_path.parent / "missing.mp4")],
        )
        self.assertEqual(result["checks"]["mlt_services"]["status"], "failed")
        self.assertEqual(
            result["checks"]["mlt_services"]["missing"],
            {"filter": ["fixture_missing_filter"]},
        )
        self.assertEqual(
            result["checks"]["mlt_services"]["required"]["producer"],
            ["avformat-novalidate", "color"],
        )

    def test_constant_and_same_direction_speed_maps_use_owned_mlt_primitives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            path = root / "speed.mlt"
            created = create_project(
                {"project_path": str(path), "clips": [{"path": str(media)}]}
            )
            sped = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "set_clip_speed",
                            "track": "V1",
                            "item_index": 0,
                            "speed": 2,
                            "pitch_compensation": True,
                        }
                    ],
                }
            )
            self.assertEqual(
                sped["project"]["tracks"][0]["items"][0]["duration_frames"], 150
            )
            producer_id = sped["project"]["tracks"][0]["items"][0]["producer_id"]
            producer = ProjectDocument.load(path).id_map()[producer_id]
            props = {
                item.get("name"): item.text for item in producer.findall("property")
            }
            self.assertEqual(props["mlt_service"], "timewarp")
            self.assertEqual(props["warp_speed"], "2")

            # A fresh source proves timeremap without speculatively merging timewarp.
            second_path = root / "ramp.mlt"
            second = create_project(
                {"project_path": str(second_path), "clips": [{"path": str(media)}]}
            )
            ramped = edit_project(
                {
                    "project_path": str(second_path),
                    "expected_revision": second["revision"],
                    "operations": [
                        {
                            "op": "set_clip_speed_map",
                            "track": "V1",
                            "item_index": 0,
                            "keyframes": [
                                {"frame": 0, "speed": 1},
                                {"frame": 100, "speed": 2},
                            ],
                        }
                    ],
                }
            )
            self.assertEqual(ramped["operation_results"][0]["duration_frames"], 175)
            document = ProjectDocument.load(second_path)
            producer_id = ramped["project"]["tracks"][0]["items"][0]["producer_id"]
            chain = document.id_map()[producer_id]
            self.assertEqual(chain.tag, "chain")
            self.assertEqual(
                next(
                    prop.text
                    for prop in chain.find("link").findall("property")
                    if prop.get("name") == "speed_map"
                ),
                "0=1;100=2",
            )

            reverse_path = root / "reverse-ramp.mlt"
            reverse = create_project(
                {"project_path": str(reverse_path), "clips": [{"path": str(media)}]}
            )
            reverse = edit_project(
                {
                    "project_path": str(reverse_path),
                    "expected_revision": reverse["revision"],
                    "operations": [
                        {
                            "op": "set_clip_speed_map",
                            "track": "V1",
                            "item_index": 0,
                            "keyframes": [
                                {"frame": 0, "speed": -1},
                                {"frame": 100, "speed": -2},
                            ],
                        }
                    ],
                }
            )
            self.assertEqual(reverse["operation_results"][0]["duration_frames"], 175)
            document = ProjectDocument.load(reverse_path)
            reverse_item = reverse["project"]["tracks"][0]["items"][0]
            chain = document.id_map()[reverse_item["producer_id"]]
            link = chain.find("link")
            assert link is not None
            self.assertEqual(chain.get("in"), "299")
            self.assertEqual(chain.get("out"), "473")
            self.assertEqual(reverse_item["in_frame"], 299)
            self.assertEqual(reverse_item["out_frame"], 473)
            self.assertEqual(
                next(
                    prop.text
                    for prop in link.findall("property")
                    if prop.get("name") == "speed_map"
                ),
                "0=-1;100=-2",
            )

            with self.assertRaisesRegex(ToolError, "same playback direction"):
                edit_project(
                    {
                        "project_path": str(reverse_path),
                        "expected_revision": reverse["revision"],
                        "operations": [
                            {
                                "op": "set_clip_speed_map",
                                "track": "V1",
                                "item_index": 0,
                                "keyframes": [
                                    {"frame": 0, "speed": -1},
                                    {"frame": 100, "speed": 1},
                                ],
                            }
                        ],
                    }
                )

    def test_slide_and_non_ripple_trim_preserve_total_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            path = root / "timeline.mlt"
            created = create_project({"project_path": str(path)})
            clips = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_clip",
                            "track": "V1",
                            "path": str(media),
                            "in_frame": 50,
                            "out_frame": 99,
                        }
                        for _ in range(3)
                    ],
                }
            )
            slid = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": clips["revision"],
                    "operations": [
                        {
                            "op": "slide_item",
                            "track": "V1",
                            "item_index": 1,
                            "delta": 10,
                        }
                    ],
                }
            )
            self.assertEqual(slid["project"]["duration_frames"], 150)
            items = slid["project"]["tracks"][0]["items"]
            self.assertEqual([item["duration_frames"] for item in items], [60, 50, 40])

            trimmed = edit_project(
                {
                    "project_path": str(path),
                    "expected_revision": slid["revision"],
                    "operations": [
                        {
                            "op": "trim_item",
                            "track": "V1",
                            "item_index": 2,
                            "edge": "end",
                            "delta": -10,
                            "ripple": False,
                        }
                    ],
                }
            )
            self.assertEqual(trimmed["project"]["duration_frames"], 150)
            self.assertEqual(
                trimmed["project"]["tracks"][0]["items"][-1]["type"], "gap"
            )

    def test_ripple_trim_updates_all_unlocked_tracks_and_preserves_fixture_xml(
        self,
    ) -> None:
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "shotcut-26.6"
            / "multitrack-ripple.mlt"
        )
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "project.mlt"
            shutil.copy2(fixture, project_path)
            snapshot = ProjectDocument.load(project_path).snapshot()
            edited = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": snapshot["revision"],
                    "operations": [
                        {
                            "op": "trim_item",
                            "track": "V1",
                            "item_index": 0,
                            "edge": "end",
                            "delta": -10,
                            "ripple": True,
                            "ripple_scope": "all_unlocked",
                            "ripple_markers": True,
                        }
                    ],
                }
            )

            tracks = {track["name"]: track for track in edited["project"]["tracks"]}
            self.assertEqual(tracks["V1"]["duration_frames"], 50)
            self.assertEqual(tracks["A1"]["duration_frames"], 50)
            self.assertEqual(tracks["V2"]["duration_frames"], 60)
            self.assertEqual(edited["project"]["markers"][0]["start_frame"], 50)
            self.assertEqual(edited["operation_results"][0]["ripple_track_count"], 1)

            root = ET.parse(project_path).getroot()
            tractor = root.find("tractor[@id='tractor0']")
            producer = root.find("producer[@id='media_v1']")
            assert tractor is not None and producer is not None
            self.assertEqual(
                next(
                    prop.text
                    for prop in tractor.findall("property")
                    if prop.get("name") == "shotcut:fixtureKeep"
                ),
                "preserved",
            )
            self.assertIsNotNone(producer.find("filter[@id='fixture_filter']"))

    def test_allowed_roots_cover_media_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            media = root / "outside.mp4"
            media.write_bytes(b"media")
            with (
                patch.dict(os.environ, {"SHOTCUT_MCP_ALLOWED_ROOTS": str(allowed)}),
                self.assertRaisesRegex(ToolError, "allowed roots"),
            ):
                create_project(
                    {
                        "project_path": str(allowed / "project.mlt"),
                        "clips": [{"path": str(media)}],
                    }
                )

    def test_resource_policy_sees_timewarp_and_filter_resource_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remote.mlt"
            path.write_text(
                "<mlt><producer><property name='warp_resource'>https://x/a.mp4</property>"
                "<property name='src'>smb://host/b.png</property></producer></mlt>",
                encoding="utf-8",
            )
            self.assertEqual(
                project_network_resources(path),
                ["https://x/a.mp4", "smb://host/b.png"],
            )

    def test_missing_media_uses_shotcut_hash_before_basename(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            original = root / "original.mp4"
            original.write_bytes(b"same-content")
            project_path = root / "missing.mlt"
            create_project(
                {
                    "project_path": str(project_path),
                    "clips": [{"path": str(original)}],
                }
            )
            original.unlink()
            search = root / "search"
            search.mkdir()
            replacement = search / "renamed.mov"
            replacement.write_bytes(b"same-content")
            result = diagnose_missing_media(
                {
                    "project_path": str(project_path),
                    "search_roots": [str(search)],
                }
            )
            candidate = result["resources"][0]["candidates"][0]
            self.assertEqual(candidate["match"], "shotcut_hash")
            self.assertTrue(candidate["verified"])

    def test_contact_sheet_sampling_crosses_the_project_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            project_path = root / "contact-sheet.mlt"
            create_project(
                {
                    "project_path": str(project_path),
                    "clips": [{"path": str(media)}],
                }
            )
            duration = ProjectDocument.load(project_path).snapshot()["duration_frames"]
            with patch(
                "shotcut_mcp.project._render_contact_sheet",
                return_value={"created": True, "path": str(root / "sheet.png")},
            ) as render:
                result = render_contact_sheet_tool(
                    {
                        "project_path": str(project_path),
                        "output_path": str(root / "sheet.png"),
                        "sample_count": 4,
                    }
                )
            self.assertTrue(result["created"])
            self.assertEqual(
                render.call_args.args[2],
                [round(index * (duration - 1) / 3) for index in range(4)],
            )

    def test_contact_sheet_can_use_managed_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            project_path = root / "managed-contact-sheet.mlt"
            create_project({"project_path": str(project_path)})
            with patch(
                "shotcut_mcp.project._render_contact_sheet",
                return_value={"created": True, "path": str(root / "managed.png")},
            ) as render:
                render_contact_sheet_tool(
                    {"project_path": str(project_path), "sample_count": 1}
                )
            self.assertIsNone(render.call_args.args[1])

    def test_duplicate_replace_filter_order_and_marker_update_are_transactional(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            source = root / "source.mp4"
            replacement = root / "replacement.mp4"
            source.write_bytes(b"source")
            replacement.write_bytes(b"replacement")
            project_path = root / "primitives.mlt"
            created = create_project(
                {"project_path": str(project_path), "clips": [{"path": str(source)}]}
            )
            prepared = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_filter",
                            "target": "clip",
                            "track": "V1",
                            "item_index": 0,
                            "service": "brightness",
                        },
                        {
                            "op": "add_filter",
                            "target": "clip",
                            "track": "V1",
                            "item_index": 0,
                            "service": "volume",
                        },
                        {
                            "op": "add_marker",
                            "start_frame": 30,
                            "text": "Draft",
                            "color": "#00A0FF",
                        },
                    ],
                }
            )
            first_filter = prepared["operation_results"][0]["filter_id"]
            second_filter = prepared["operation_results"][1]["filter_id"]
            marker_id = prepared["operation_results"][2]["marker_id"]
            operations = [
                {
                    "op": "move_filter",
                    "filter_id": second_filter,
                    "before_filter_id": first_filter,
                },
                {"op": "duplicate_item", "track": "V1", "item_index": 0},
                {
                    "op": "replace_item_media",
                    "track": "V1",
                    "item_index": 0,
                    "path": str(replacement),
                    "caption": "New take",
                },
                {
                    "op": "update_marker",
                    "marker_id": marker_id,
                    "start_frame": 60,
                    "text": "Approved",
                    "color": "#FF8800",
                },
            ]
            before_plan = project_path.read_bytes()
            planned = plan_project_edit(
                {
                    "project_path": str(project_path),
                    "expected_revision": prepared["revision"],
                    "operations": operations,
                }
            )
            self.assertTrue(planned["planned"])
            self.assertEqual(project_path.read_bytes(), before_plan)
            changed = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": prepared["revision"],
                    "operations": operations,
                }
            )
            items = changed["project"]["tracks"][0]["items"]
            self.assertEqual(len(items), 2)
            self.assertNotEqual(items[0]["producer_id"], items[1]["producer_id"])
            self.assertEqual(
                [item["service"] for item in items[0]["filters"]],
                ["volume", "brightness"],
            )
            self.assertEqual(
                [item["filter_index"] for item in items[0]["filters"]], [0, 1]
            )
            self.assertEqual(
                [item["service"] for item in items[1]["filters"]],
                ["volume", "brightness"],
            )
            self.assertEqual(items[0]["caption"], "New take")
            self.assertEqual(
                Path(items[0]["resource"]).resolve(), replacement.resolve()
            )
            marker = changed["project"]["markers"][0]
            self.assertEqual(marker["marker_id"], marker_id)
            self.assertEqual(marker["start_frame"], 60)
            self.assertEqual(marker["end_frame"], 60)
            self.assertEqual(marker["text"], "Approved")
            self.assertEqual(marker["color"], "#FF8800")

    def test_still_images_use_qimage_across_creation_and_replacement(self) -> None:
        def probe(path: Path) -> dict[str, object]:
            if path.suffix.lower() == ".png":
                return {
                    "format": {},
                    "streams": [{"codec_type": "video", "codec_name": "png"}],
                }
            return {
                "format": {"duration": "10"},
                "streams": [{"codec_type": "video", "codec_name": "h264"}],
            }

        def producer_properties(path: Path, producer_id: str) -> dict[str, str]:
            producer = next(
                element
                for element in ET.parse(path).getroot().findall("producer")
                if element.get("id") == producer_id
            )
            return {
                prop.get("name", ""): prop.text or ""
                for prop in producer.findall("property")
            }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("shotcut_mcp.project_document.probe_media_raw", side_effect=probe),
        ):
            root = Path(directory)
            video = root / "source.mp4"
            first_image = root / "first.png"
            second_image = root / "second.png"
            for path in (video, first_image, second_image):
                path.write_bytes(path.name.encode())

            image_project = root / "image.mlt"
            created = create_project(
                {
                    "project_path": str(image_project),
                    "clips": [
                        {
                            "path": str(first_image),
                            "image_duration_seconds": 2,
                        }
                    ],
                }
            )
            item = created["project"]["tracks"][0]["items"][0]
            self.assertEqual(
                producer_properties(image_project, item["producer_id"])["mlt_service"],
                "qimage",
            )

            replaced_image = edit_project(
                {
                    "project_path": str(image_project),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "replace_item_media",
                            "track": "V1",
                            "item_index": 0,
                            "path": str(second_image),
                        }
                    ],
                }
            )
            item = replaced_image["project"]["tracks"][0]["items"][0]
            self.assertEqual(
                producer_properties(image_project, item["producer_id"])["mlt_service"],
                "qimage",
            )

            replaced_video = edit_project(
                {
                    "project_path": str(image_project),
                    "expected_revision": replaced_image["revision"],
                    "operations": [
                        {
                            "op": "replace_item_media",
                            "track": "V1",
                            "item_index": 0,
                            "path": str(video),
                        }
                    ],
                }
            )
            item = replaced_video["project"]["tracks"][0]["items"][0]
            self.assertEqual(
                producer_properties(image_project, item["producer_id"])["mlt_service"],
                "avformat-novalidate",
            )

            video_project = root / "video.mlt"
            video_created = create_project(
                {
                    "project_path": str(video_project),
                    "clips": [{"path": str(video), "in_frame": 120, "out_frame": 179}],
                }
            )
            filtered = edit_project(
                {
                    "project_path": str(video_project),
                    "expected_revision": video_created["revision"],
                    "operations": [
                        {
                            "op": "add_filter",
                            "target": "clip",
                            "track": "V1",
                            "item_index": 0,
                            "service": "brightness",
                            "in_frame": 130,
                            "out_frame": 150,
                        }
                    ],
                }
            )
            filter_id = filtered["operation_results"][0]["filter_id"]
            replaced_still = edit_project(
                {
                    "project_path": str(video_project),
                    "expected_revision": filtered["revision"],
                    "operations": [
                        {
                            "op": "replace_item_media",
                            "track": "V1",
                            "item_index": 0,
                            "path": str(first_image),
                        }
                    ],
                }
            )
            item = replaced_still["project"]["tracks"][0]["items"][0]
            self.assertEqual((item["in_frame"], item["out_frame"]), (120, 179))
            self.assertEqual(item["filters"][0]["filter_id"], filter_id)
            self.assertEqual(
                producer_properties(video_project, item["producer_id"])["mlt_service"],
                "qimage",
            )

    def test_set_clip_opacity_reuses_one_owned_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "opacity.mlt"
            created = create_project({"project_path": str(project_path)})
            generated = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_generator",
                            "track": "V1",
                            "generator": "color",
                            "duration_frames": 25,
                            "color": "#ff0000",
                        }
                    ],
                }
            )
            animated = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": generated["revision"],
                    "operations": [
                        {
                            "op": "set_clip_opacity",
                            "track": "V1",
                            "item_index": 0,
                            "opacity_keyframes": [
                                {"frame": 0, "opacity": 0},
                                {"frame": 12, "opacity": 1},
                                {"frame": 24, "opacity": 0},
                            ],
                        }
                    ],
                }
            )
            operation_result = animated["operation_results"][0]
            filters = animated["project"]["tracks"][0]["items"][0]["filters"]
            self.assertEqual(len(filters), 1)
            self.assertEqual(operation_result["filter_id"], filters[0]["filter_id"])
            self.assertEqual(filters[0]["service"], "brightness")
            self.assertEqual(filters[0]["properties"]["level"], "1")
            self.assertEqual(filters[0]["properties"]["alpha"], "0=0;12=1;24=0")
            self.assertEqual(filters[0]["properties"]["shotcut:mcpOpacity"], "1")

            updated = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": animated["revision"],
                    "operations": [
                        {
                            "op": "set_clip_opacity",
                            "track": "V1",
                            "item_index": 0,
                            "opacity_keyframes": [
                                {"frame": 0, "opacity": 1},
                                {"frame": 24, "opacity": 0},
                            ],
                            "interpolation": "smooth",
                        }
                    ],
                }
            )
            filters = updated["project"]["tracks"][0]["items"][0]["filters"]
            self.assertEqual(len(filters), 1)
            self.assertEqual(filters[0]["filter_id"], operation_result["filter_id"])
            self.assertEqual(filters[0]["properties"]["alpha"], "0~=1;24~=0")

            before = project_path.read_bytes()
            with self.assertRaisesRegex(ToolError, "between 0 and 1"):
                edit_project(
                    {
                        "project_path": str(project_path),
                        "expected_revision": updated["revision"],
                        "operations": [
                            {
                                "op": "set_clip_opacity",
                                "track": "V1",
                                "item_index": 0,
                                "opacity_keyframes": [{"frame": 0, "opacity": 1.1}],
                            }
                        ],
                    }
                )
            self.assertEqual(project_path.read_bytes(), before)

    def test_media_replacement_rejects_adjacent_transition_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, self._media_patch():
            root = Path(directory)
            first = root / "first.mp4"
            second = root / "second.mp4"
            replacement = root / "replacement.mp4"
            for path in (first, second, replacement):
                path.write_bytes(path.name.encode())
            project_path = root / "transition-replace.mlt"
            created = create_project(
                {
                    "project_path": str(project_path),
                    "clips": [{"path": str(first)}, {"path": str(second)}],
                }
            )
            transitioned = edit_project(
                {
                    "project_path": str(project_path),
                    "expected_revision": created["revision"],
                    "operations": [
                        {
                            "op": "add_transition",
                            "track": "V1",
                            "left_item_index": 0,
                            "duration_frames": 10,
                        }
                    ],
                }
            )
            before = project_path.read_bytes()
            with self.assertRaisesRegex(ToolError, "adjacent transition"):
                edit_project(
                    {
                        "project_path": str(project_path),
                        "expected_revision": transitioned["revision"],
                        "operations": [
                            {
                                "op": "replace_item_media",
                                "track": "V1",
                                "item_index": 0,
                                "path": str(replacement),
                            }
                        ],
                    }
                )
            self.assertEqual(project_path.read_bytes(), before)


class BacklogPlatformFeatureTests(unittest.TestCase):
    def test_batch_preview_reports_per_item_failures(self) -> None:
        project = Path("project.mlt")
        with patch(
            "shotcut_mcp.platform.render_preview",
            side_effect=[
                {"created": True, "path": "one.png", "frame": 1, "size_bytes": 3},
                ToolError("failed"),
            ],
        ):
            result = platform.render_preview_batch(
                project, [(1, Path("one.png")), (2, Path("two.png"))]
            )
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["results"][1]["error"], "failed")

    def test_hardware_encoder_detection_distinguishes_advertised_from_working(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ffmpeg = Path(directory) / "ffmpeg"
            ffmpeg.write_bytes(b"binary")

            def run(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if "-encoders" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        " V..... h264_nvenc NVIDIA\n V..... h264_qsv Intel\n",
                        "",
                    )
                output = Path(command[-1])
                if "h264_nvenc" in command:
                    output.write_bytes(b"encoded")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 1, "", "device unavailable")

            platform._ENCODER_CACHE.clear()
            with (
                patch(
                    "shotcut_mcp.platform.discover_executables",
                    return_value=SimpleNamespace(ffmpeg=ffmpeg),
                ),
                patch("shotcut_mcp.platform.run_capture", side_effect=run),
            ):
                result = platform.detect_hardware_encoders(refresh=True)
            states = {item["encoder"]: item["state"] for item in result["candidates"]}
            self.assertEqual(states["h264_nvenc"], "smoke_tested")
            self.assertEqual(states["h264_qsv"], "advertised")

    def test_process_capture_enforces_output_budget(self) -> None:
        with self.assertRaisesRegex(ToolError, "output limit"):
            platform.run_capture(
                [os.sys.executable, "-c", "print('x' * 10000)"],
                max_output_bytes=1024,
            )


class RenderMonitoringFeatureTests(unittest.TestCase):
    def test_render_log_is_bounded_and_supervisor_is_reaped_without_polling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            renderer = root / "noisy.py"
            output = root / "output.mp4"
            renderer.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "sys.stdout.write('x' * 800000)\n"
                "sys.stdout.flush()\n"
                "target = next(a[9:] for a in sys.argv if a.startswith('avformat:'))\n"
                "Path(target).write_bytes(b'rendered')\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "shotcut_mcp.render.discover_executables",
                    return_value=SimpleNamespace(melt=Path(sys.executable)),
                ),
                patch(
                    "shotcut_mcp.render.require_executable",
                    return_value=Path(sys.executable),
                ),
                patch("shotcut_mcp.render.ensure_melt_ready"),
            ):
                job = render_module.start_render(
                    {
                        "project_path": str(renderer),
                        "output_path": str(output),
                    }
                )
            job_id = str(job["job_id"])
            worker = render_module.RUNNING_JOBS[job_id]
            worker.wait(timeout=15)
            time.sleep(1.2)
            self.assertNotIn(job_id, render_module.RUNNING_JOBS)
            status = render_module.render_status(job_id)
            self.assertEqual(status["status"], "completed")
            self.assertLessEqual(Path(status["log_path"]).stat().st_size, 512 * 1024)

    def test_history_is_paginated_and_running_job_has_eta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_dir = Path(directory) / "jobs"
            with patch.object(render_jobs, "JOB_DIR", job_dir):
                now = 10_000.0
                for index in range(3):
                    job_id = f"{index + 1:032x}"
                    render_jobs.write_job(
                        {
                            "job_id": job_id,
                            "status": "completed",
                            "project_path": "project.mlt",
                            "output_path": str(Path(directory) / f"{index}.mp4"),
                            "log_path": str(render_jobs.log_path(job_id)),
                            "started_at": now + index,
                            "finished_at": now + index + 1,
                        }
                    )
                first = render_module.list_render_jobs({"limit": 2})
                self.assertEqual(first["count"], 2)
                self.assertIsNotNone(first["next_cursor"])
                second = render_module.list_render_jobs(
                    {"limit": 2, "cursor": first["next_cursor"]}
                )
                self.assertEqual(second["count"], 1)

                active_id = "f" * 32
                render_jobs.write_job(
                    {
                        "job_id": active_id,
                        "status": "running",
                        "worker_pid": None,
                        "project_path": "project.mlt",
                        "output_path": str(Path(directory) / "active.mp4"),
                        "log_path": str(render_jobs.log_path(active_id)),
                        "started_at": time.time(),
                        "progress_percent": 20,
                        "progress_samples": [
                            {"at": time.time() - 5, "percent": 10, "frame": 10},
                            {"at": time.time(), "percent": 20, "frame": 20},
                        ],
                    }
                )
                status = render_module.render_status(active_id)
                self.assertIsNotNone(status["eta_seconds"])
                self.assertEqual(status["eta_basis"], "smoothed_progress_percent")


if __name__ == "__main__":
    unittest.main()
