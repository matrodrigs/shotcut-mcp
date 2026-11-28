from __future__ import annotations

import copy
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp.errors import ToolError
from shotcut_mcp.project import ProjectDocument, edit_project, plan_project_edit
from shotcut_mcp.protocol import schema_errors
from shotcut_mcp.server import ProtocolSession, handle_request, serve, write_message
from shotcut_mcp.silence import plan_silence_cuts
from shotcut_mcp.storage import managed_preview_path
from shotcut_mcp.tools import TOOLS


class ProjectViewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "timeline.mlt"

    def test_snapshot_leaves_file_status_unknown_for_restricted_media(self) -> None:
        self.make_project()
        document = ProjectDocument.load(self.path)
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "source.mp4"
            media.touch()
            producer = document.root.find("producer")
            resource = producer.find("property[@name='resource']")
            resource.text = str(media)
            with patch.dict(os.environ, {"SHOTCUT_MCP_ALLOWED_ROOTS": str(self.root)}):
                references = document.snapshot()["resources"]
            entry = next(item for item in references if item["resource"] == str(media))
            self.assertIsNone(entry["exists"])
            self.assertIsNone(entry["resolved_path"])

    def make_project(self, count: int = 8) -> str:
        document = ProjectDocument.new(
            self.path,
            width=64,
            height=64,
            fps_num=30,
            fps_den=1,
            title="Original notes",
        )
        document.add_generator(
            {
                "track": "V1",
                "generator": "color",
                "color": "#ff0000",
                "duration_frames": 10,
            }
        )
        playlist = document.tracks()[0].playlist
        entry = playlist.find("entry")
        for _ in range(count - 1):
            playlist.append(copy.deepcopy(entry))
        document.update_main_duration()
        self.path.write_bytes(document.to_bytes())
        return ProjectDocument.load(self.path).revision

    def call(
        self,
        name: str,
        arguments: dict[str, object],
        version: str = "2025-11-25",
        request_id: object = 1,
    ) -> dict:
        return handle_request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            ProtocolSession(protocol_version=version),
        )

    def test_planned_review_preserves_previous_artifact_on_failure(self) -> None:
        revision = self.make_project()
        target = managed_preview_path(self.path, "plan.mp4")
        target.write_bytes(b"previous review")
        arguments = {
            "project_path": str(self.path),
            "expected_revision": revision,
            "operations": [{"op": "set_notes", "notes": "changed"}],
            "review": {"in_frame": 0, "out_frame": 3},
        }

        def racing_review(_args, *, staged_output):
            staged_output.write_bytes(b"new review")
            self.path.write_bytes(self.path.read_bytes() + b" ")
            return {"created": True}

        with (
            patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": False},
            ),
            self.assertRaises(ToolError) as rejected,
        ):
            plan_project_edit(arguments)
        self.assertEqual(rejected.exception.code, "project_validation_failed")
        self.assertEqual(target.read_bytes(), b"previous review")
        with (
            patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True},
            ),
            patch("shotcut_mcp.project.render_review_clip", side_effect=racing_review),
            self.assertRaises(ToolError) as conflict,
        ):
            plan_project_edit(arguments)
        self.assertEqual(conflict.exception.code, "project_revision_conflict")
        self.assertEqual(target.read_bytes(), b"previous review")

    def test_silence_plan_distinguishes_fully_silent_range_from_audible_last_frame(
        self,
    ) -> None:
        revision = self.make_project(count=12)
        arguments = {
            "project_path": str(self.path),
            "expected_revision": revision,
            "margin_seconds": 0,
        }
        for end, length in ((4.0, 0), (119 / 30, 119)):
            with (
                self.subTest(end=end),
                patch(
                    "shotcut_mcp.silence.timeline_silences", return_value=[(0.0, end)]
                ),
                patch(
                    "shotcut_mcp.silence.preview_edit_operations",
                    return_value={"review": {"created": True}},
                ),
            ):
                result = plan_silence_cuts(arguments)
                self.assertEqual(
                    sum(cut["duration_frames"] for cut in result["cuts"]), length
                )
                self.assertEqual(bool(result["review"]), bool(length))

    def test_pages_preserve_item_identity_and_reject_changed_revision(self) -> None:
        revision = self.make_project()
        expected = ProjectDocument.load(self.path).snapshot()["tracks"][0]["items"]
        gathered = []
        offset = 0
        while True:
            result = self.call(
                "inspect_project",
                {
                    "path": str(self.path),
                    "limit": 3,
                    "offset": offset,
                    "expected_revision": revision,
                },
            )["result"]
            self.assertFalse(result["isError"])
            payload = result["structuredContent"]
            schema = next(
                tool["outputSchema"]
                for tool in TOOLS
                if tool["name"] == "inspect_project"
            )
            self.assertEqual(schema_errors(payload, schema), [])
            gathered.extend(
                item for track in payload["tracks"] for item in track["items"]
            )
            offset = payload["pagination"]["next_offset"]
            if offset is None:
                break
        self.assertEqual(gathered, expected)
        self.path.write_bytes(
            self.path.read_bytes().replace(b"Original notes", b"Changed notes")
        )
        result = self.call(
            "inspect_project",
            {"path": str(self.path), "offset": 3, "expected_revision": revision},
        )["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error_code"], "project_revision_conflict"
        )

    def test_summary_and_frame_filter_keep_original_indexes(self) -> None:
        self.make_project()
        result = self.call(
            "inspect_project",
            {
                "path": str(self.path),
                "detail": "summary",
                "track": "V1",
                "in_frame": 19,
                "out_frame": 30,
            },
        )["result"]["structuredContent"]
        self.assertIsNone(result["notes"])
        self.assertEqual(result["tracks"][0]["properties"], {})
        self.assertEqual(
            [item["item_index"] for item in result["tracks"][0]["items"]], [1, 2, 3]
        )
        self.assertEqual(
            ProjectDocument.load(self.path).snapshot()["notes"], "Original notes"
        )
        invalid = self.call(
            "inspect_project", {"path": str(self.path), "in_frame": 30, "out_frame": 19}
        )
        self.assertTrue(invalid["result"]["isError"])

    def test_large_inspection_fits_default_transport_budget(self) -> None:
        self.make_project(20_000)
        response = self.call("inspect_project", {"path": str(self.path)})
        output = io.BytesIO()
        write_message(response, output)
        wire = json.loads(output.getvalue())
        self.assertNotIn("error", wire)
        payload = wire["result"]["structuredContent"]
        self.assertEqual(sum(len(track["items"]) for track in payload["tracks"]), 100)
        self.assertEqual(payload["pagination"]["totals"]["items"], 20_000)
        self.assertEqual(payload["pagination"]["next_offset"], 100)

    def test_committed_edit_receipt_survives_small_message_budget(self) -> None:
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            with self.subTest(version=version):
                revision = self.make_project(500)
                requests = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": version},
                    },
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "edit_project",
                            "arguments": {
                                "project_path": str(self.path),
                                "expected_revision": revision,
                                "operations": [
                                    {"op": "set_notes", "notes": "Committed notes"}
                                ],
                            },
                        },
                    },
                ]
                output = io.BytesIO()
                with (
                    patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "65536"}),
                    patch(
                        "shotcut_mcp.project.validate_project_file",
                        return_value={
                            "valid": True,
                            "return_code": 0,
                            "diagnostic": "",
                        },
                    ),
                ):
                    serve(
                        io.BytesIO(
                            b"".join(
                                (json.dumps(request) + "\n").encode()
                                for request in requests
                            )
                        ),
                        output,
                    )
                responses = [
                    json.loads(line) for line in output.getvalue().splitlines()
                ]
                reply = next(response for response in responses if response["id"] == 2)
                self.assertNotIn("error", reply)
                result = reply["result"]
                self.assertFalse(result["isError"])
                payload = result.get("structuredContent") or json.loads(
                    result["content"][0]["text"]
                )
                self.assertTrue(payload["edited"])
                self.assertTrue(payload["result_truncated"])
                saved = ProjectDocument.load(self.path)
                self.assertEqual(payload["revision"], saved.revision)
                self.assertEqual(saved.snapshot()["notes"], "Committed notes")
                schema = next(
                    tool["outputSchema"]
                    for tool in TOOLS
                    if tool["name"] == "edit_project"
                )
                self.assertEqual(schema_errors(payload, schema), [])
                self.assertTrue(
                    all(
                        len(line) + 1 <= 65536
                        for line in output.getvalue().splitlines()
                    )
                )

    def test_long_request_id_cannot_hide_committed_success(self) -> None:
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            for id_length in (1, 650, 800):
                with self.subTest(version=version, id_length=id_length):
                    revision = self.make_project()
                    with (
                        patch.dict(
                            os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "1024"}
                        ),
                        patch(
                            "shotcut_mcp.project.validate_project_file",
                            return_value={
                                "valid": True,
                                "return_code": 0,
                                "diagnostic": "",
                            },
                        ),
                    ):
                        response = self.call(
                            "edit_project",
                            {
                                "project_path": str(self.path),
                                "expected_revision": revision,
                                "operations": [{"op": "set_notes", "notes": "Saved"}],
                            },
                            version,
                            "x" * id_length,
                        )
                        output = io.BytesIO()
                        write_message(response, output)
                    reply = json.loads(output.getvalue())
                    changed = ProjectDocument.load(self.path).revision != revision
                    if changed:
                        self.assertNotIn("error", reply)
                        self.assertFalse(reply["result"]["isError"])
                    else:
                        self.assertIn("error", reply)
                    self.assertLessEqual(len(output.getvalue()), 1024)

    def test_creation_and_restore_keep_receipts_when_validation_details_are_large(
        self,
    ) -> None:
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            with (
                self.subTest(version=version),
                patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "4096"}),
                patch(
                    "shotcut_mcp.project.validate_project_file",
                    return_value={
                        "valid": True,
                        "return_code": 0,
                        "diagnostic": "validation detail " * 1000,
                    },
                ),
            ):
                path = self.root / f"receipt-{version}.mlt"
                created = self.call(
                    "create_project",
                    {"project_path": str(path), "notes": "Original"},
                    version,
                )
                saved = ProjectDocument.load(path)
                edited = edit_project(
                    {
                        "project_path": str(path),
                        "expected_revision": saved.revision,
                        "operations": [{"op": "set_notes", "notes": "Changed"}],
                    }
                )
                restored = self.call(
                    "restore_project_backup",
                    {
                        "project_path": str(path),
                        "backup_path": edited["backup_path"],
                        "expected_revision": edited["revision"],
                    },
                    version,
                )
                self.assertEqual(
                    ProjectDocument.load(path).snapshot()["notes"], "Original"
                )
                for name, flag, response in (
                    ("create_project", "created", created),
                    ("restore_project_backup", "restored", restored),
                ):
                    output = io.BytesIO()
                    write_message(response, output)
                    wire = json.loads(output.getvalue())
                    self.assertNotIn("error", wire)
                    result = wire["result"]
                    self.assertFalse(result["isError"])
                    payload = result.get("structuredContent") or json.loads(
                        result["content"][0]["text"]
                    )
                    self.assertTrue(payload[flag])
                    self.assertTrue(payload["result_truncated"])
                    self.assertEqual(payload["revision"], saved.revision)
                    schema = next(
                        tool["outputSchema"] for tool in TOOLS if tool["name"] == name
                    )
                    self.assertEqual(schema_errors(payload, schema), [])
                    self.assertLessEqual(len(output.getvalue()), 4096)

    def test_legacy_batch_preserves_each_committed_edit_receipt(self) -> None:
        batch = []
        projects = {}
        for request_id in (2, 3):
            self.path = self.root / f"batch-{request_id}.mlt"
            revision = self.make_project(75)
            projects[request_id] = self.path
            batch.append(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "edit_project",
                        "arguments": {
                            "project_path": str(self.path),
                            "expected_revision": revision,
                            "operations": [{"op": "set_notes", "notes": "Batch saved"}],
                        },
                    },
                }
            )
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            batch,
        ]
        output = io.BytesIO()
        with (
            patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "65536"}),
            patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True, "return_code": 0, "diagnostic": ""},
            ),
        ):
            serve(
                io.BytesIO(
                    b"".join(
                        (json.dumps(message) + "\n").encode() for message in messages
                    )
                ),
                output,
            )

        replies = json.loads(output.getvalue().splitlines()[-1])
        self.assertIsInstance(replies, list)
        self.assertEqual({reply["id"] for reply in replies}, set(projects))
        for reply in replies:
            self.assertNotIn("error", reply)
            self.assertFalse(reply["result"]["isError"])
            payload = json.loads(reply["result"]["content"][0]["text"])
            self.assertTrue(payload["edited"])
            saved = ProjectDocument.load(projects[reply["id"]])
            self.assertEqual(saved.snapshot()["notes"], "Batch saved")
            self.assertEqual(payload["revision"], saved.revision)
        self.assertTrue(
            all(len(line) + 1 <= 65536 for line in output.getvalue().splitlines())
        )

    def test_legacy_batch_rejects_mutations_when_minimum_receipts_cannot_fit(
        self,
    ) -> None:
        paths = [self.root / f"new-{index}.mlt" for index in range(4)]
        batch = [
            {
                "jsonrpc": "2.0",
                "id": index + 2,
                "method": "tools/call",
                "params": {
                    "name": "create_project",
                    "arguments": {"project_path": str(path)},
                },
            }
            for index, path in enumerate(paths)
        ]
        self.assertLessEqual(len((json.dumps(batch) + "\n").encode()), 1024)
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            batch,
        ]
        output = io.BytesIO()
        with (
            patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "1024"}),
            patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True, "return_code": 0, "diagnostic": ""},
            ),
        ):
            serve(
                io.BytesIO(
                    b"".join(
                        (json.dumps(message) + "\n").encode() for message in messages
                    )
                ),
                output,
            )

        self.assertFalse(any(path.exists() for path in paths))
        result = json.loads(output.getvalue().splitlines()[-1])
        replies = result if isinstance(result, list) else [result]
        self.assertTrue(all("error" in reply for reply in replies))
        self.assertTrue(
            all(len(line) + 1 <= 1024 for line in output.getvalue().splitlines())
        )

    def test_legacy_notification_batch_does_not_reserve_response_budget(self) -> None:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            [
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
                for _ in range(16)
            ],
        ]
        self.assertLessEqual(len((json.dumps(messages[-1]) + "\n").encode()), 1024)
        output = io.BytesIO()
        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "1024"}):
            serve(
                io.BytesIO(
                    b"".join(
                        (json.dumps(message) + "\n").encode() for message in messages
                    )
                ),
                output,
            )
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1])

    def test_legacy_notifications_do_not_reduce_a_mutation_receipt_budget(self) -> None:
        batch = [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "create_project",
                    "arguments": {"project_path": str(self.path)},
                },
            },
            *[
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
                for _ in range(8)
            ],
        ]
        self.assertLessEqual(len((json.dumps(batch) + "\n").encode()), 1024)
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            batch,
        ]
        output = io.BytesIO()
        with (
            patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "1024"}),
            patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={"valid": True, "return_code": 0, "diagnostic": ""},
            ),
        ):
            serve(
                io.BytesIO(
                    b"".join(
                        (json.dumps(message) + "\n").encode() for message in messages
                    )
                ),
                output,
            )

        replies = json.loads(output.getvalue().splitlines()[-1])
        self.assertIsInstance(replies, list)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 2)
        self.assertNotIn("error", replies[0])
        payload = json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertTrue(payload["created"])
        self.assertEqual(payload["revision"], ProjectDocument.load(self.path).revision)
        self.assertTrue(
            all(len(line) + 1 <= 1024 for line in output.getvalue().splitlines())
        )

    def test_every_named_edit_primitive_can_be_queried(self) -> None:
        payload = self.call("shotcut_capabilities", {})["result"]["structuredContent"]
        guidance = payload["feature_guidance"]["edit_primitives"]
        names = re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", guidance)
        self.assertTrue(names)
        for name in names:
            with self.subTest(operation=name):
                reply = self.call("shotcut_capabilities", {"operation": name})
                self.assertNotIn("error", reply)
                self.assertFalse(reply["result"]["isError"])
                self.assertEqual(
                    reply["result"]["structuredContent"]["operations"][name]["example"][
                        "op"
                    ],
                    name,
                )


if __name__ == "__main__":
    unittest.main()
