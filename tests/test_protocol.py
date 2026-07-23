from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shotcut_mcp.errors import ConflictError, RequestCancelled, ToolError
from shotcut_mcp.project import ProjectDocument
from shotcut_mcp.protocol import cancellation_requested
from shotcut_mcp.server import (
    HANDLERS,
    SERVER_INSTRUCTIONS,
    ProtocolSession,
    handle_request,
    serve,
)
from shotcut_mcp.tools import OPERATION_CATALOG, OPERATION_EXAMPLES


def request(
    method: str, params: object = None, request_id: int = 1
) -> dict[str, object]:
    message: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        message["params"] = params
    return message


class ProtocolValidationTests(unittest.TestCase):
    def test_invalid_jsonrpc_envelope_is_rejected(self) -> None:
        response = handle_request({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        self.assertEqual(response["error"]["code"], -32600)

    def test_non_object_tool_arguments_are_rejected(self) -> None:
        response = handle_request(
            request("tools/call", {"name": "shotcut_status", "arguments": []})
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_tool_input_schema_is_enforced_before_execution(self) -> None:
        response = handle_request(
            request("tools/call", {"name": "probe_media", "arguments": {}})
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("path", response["error"]["data"]["validationErrors"][0])

    def test_cancellation_notification_reaches_an_inflight_tool(self) -> None:
        started = threading.Event()

        def slow_handler(_arguments: dict[str, object]) -> dict[str, object]:
            started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cancellation_requested():
                    raise RequestCancelled("cancelled in test")
                time.sleep(0.01)
            return {"unexpected": True}

        messages = (
            json.dumps(request("initialize", {"protocolVersion": "2025-11-25"}))
            + "\n"
            + json.dumps(
                request(
                    "tools/call",
                    {"name": "shotcut_status", "arguments": {}},
                    request_id=9,
                )
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 9, "reason": "test"},
                }
            )
            + "\n"
        ).encode()
        output = io.BytesIO()
        with patch.dict(
            "shotcut_mcp.server.HANDLERS", {"shotcut_status": slow_handler}
        ):
            serve(io.BytesIO(messages), output)

        response = json.loads(output.getvalue().decode().splitlines()[-1])
        self.assertEqual(response["id"], 9)
        self.assertEqual(response["error"]["code"], -32800)

    def test_stdio_server_requires_initialize_before_tools(self) -> None:
        message = (
            json.dumps(
                request(
                    "tools/call",
                    {"name": "shotcut_status", "arguments": {}},
                )
            )
            + "\n"
        ).encode()
        output = io.BytesIO()

        serve(io.BytesIO(message), output)

        response = json.loads(output.getvalue().decode())
        self.assertEqual(response["error"]["code"], -32002)

    def test_stdio_server_bounds_the_inflight_request_queue(self) -> None:
        def slow_handler(_arguments: dict[str, object]) -> dict[str, object]:
            time.sleep(0.2)
            return {"done": True}

        messages = (
            json.dumps(request("initialize", {"protocolVersion": "2025-11-25"}))
            + "\n"
            + json.dumps(
                request(
                    "tools/call",
                    {"name": "shotcut_status", "arguments": {}},
                    request_id=10,
                )
            )
            + "\n"
            + json.dumps(
                request(
                    "tools/call",
                    {"name": "shotcut_status", "arguments": {}},
                    request_id=11,
                )
            )
            + "\n"
        ).encode()
        output = io.BytesIO()
        with (
            patch.dict(
                os.environ,
                {"SHOTCUT_MCP_MAX_PENDING": "1", "SHOTCUT_MCP_MAX_WORKERS": "1"},
            ),
            patch.dict("shotcut_mcp.server.HANDLERS", {"shotcut_status": slow_handler}),
        ):
            serve(io.BytesIO(messages), output)

        responses = [
            json.loads(line) for line in output.getvalue().decode().splitlines()
        ]
        overload = next(item for item in responses if item.get("id") == 11)
        self.assertEqual(overload["error"]["code"], -32000)

    def test_stdio_server_rejects_an_oversized_message(self) -> None:
        oversized = (json.dumps({"padding": "x" * 2_000}) + "\n").encode()
        output = io.BytesIO()

        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "1024"}):
            serve(io.BytesIO(oversized), output)

        response = json.loads(output.getvalue().decode())
        self.assertEqual(response["error"]["code"], -32600)
        self.assertIn("size limit", response["error"]["message"])


class ProtocolNegotiationTests(unittest.TestCase):
    def test_initialize_instructions_route_common_safe_workflows(self) -> None:
        response = handle_request(
            request("initialize", {"protocolVersion": "2025-11-25"})
        )
        instructions = response["result"]["instructions"]
        first_window = instructions[:512]
        self.assertEqual(instructions, SERVER_INSTRUCTIONS)
        for phrase in (
            "inspect_project",
            "render_contact_sheet",
            "expected_revision",
            "shotcut_capabilities",
            "force",
            "overwrite",
        ):
            self.assertIn(phrase, first_window)
        for phrase in (
            "analyze_media_quality",
            "inclusive frames",
            "render_status",
            "export_marker_chapters",
        ):
            self.assertIn(phrase, instructions)

    def test_2025_03_batch_requests_are_supported_only_after_negotiation(self) -> None:
        messages = (
            json.dumps(request("initialize", {"protocolVersion": "2025-03-26"}))
            + "\n"
            + json.dumps([request("ping", request_id=2), request("ping", request_id=3)])
            + "\n"
        ).encode()
        output = io.BytesIO()

        serve(io.BytesIO(messages), output)

        responses = [
            json.loads(line) for line in output.getvalue().decode().splitlines()
        ]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual([item["id"] for item in responses[1]], [2, 3])

    def test_legacy_batch_is_bounded_by_the_pending_limit(self) -> None:
        messages = (
            json.dumps(request("initialize", {"protocolVersion": "2025-03-26"}))
            + "\n"
            + json.dumps([request("ping", request_id=2), request("ping", request_id=3)])
            + "\n"
        ).encode()
        output = io.BytesIO()

        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_PENDING": "1"}):
            serve(io.BytesIO(messages), output)

        responses = [
            json.loads(line) for line in output.getvalue().decode().splitlines()
        ]
        self.assertEqual(responses[1]["error"]["code"], -32000)

    def test_legacy_client_receives_only_legacy_tool_fields(self) -> None:
        session = ProtocolSession()
        handle_request(
            request("initialize", {"protocolVersion": "2024-11-05"}), session
        )

        listed = handle_request(request("tools/list"), session)
        tool = listed["result"]["tools"][0]
        self.assertNotIn("title", tool)
        self.assertNotIn("annotations", tool)
        self.assertNotIn("outputSchema", tool)

        called = handle_request(
            request(
                "tools/call",
                {"name": "shotcut_capabilities", "arguments": {}},
            ),
            session,
        )
        self.assertNotIn("structuredContent", called["result"])

    def test_2025_03_uses_annotation_title_not_top_level_title(self) -> None:
        session = ProtocolSession()
        handle_request(
            request("initialize", {"protocolVersion": "2025-03-26"}), session
        )
        listed = handle_request(request("tools/list"), session)
        tool = listed["result"]["tools"][0]
        self.assertNotIn("title", tool)
        self.assertIn("title", tool["annotations"])
        self.assertNotIn("outputSchema", tool)

    def test_current_tools_publish_described_inputs_outputs_and_local_hints(
        self,
    ) -> None:
        listed = handle_request(request("tools/list"))
        tools = listed["result"]["tools"]
        for tool in tools:
            self.assertIn("outputSchema", tool)
            self.assertFalse(tool["annotations"]["openWorldHint"])
            properties = tool["inputSchema"].get("properties", {})
            for name, schema in properties.items():
                self.assertIn("description", schema, f"{tool['name']}.{name}")
        by_name = {tool["name"]: tool for tool in tools}
        track_kind = by_name["create_project"]["inputSchema"]["properties"]["tracks"][
            "items"
        ]["properties"]["kind"]
        self.assertEqual(track_kind["description"], "Track kind.")
        render_status = by_name["render_status"]["outputSchema"]["properties"]
        self.assertEqual(render_status["progress_percent"]["type"], ["number", "null"])
        self.assertEqual(render_status["log_tail"]["type"], "string")

    def test_capabilities_can_describe_one_operation_in_full(self) -> None:
        response = handle_request(
            request(
                "tools/call",
                {
                    "name": "shotcut_capabilities",
                    "arguments": {"operation": "trim_item"},
                },
            )
        )
        operation = response["result"]["structuredContent"]["operations"]["trim_item"]
        self.assertEqual(operation["schema"]["properties"]["delta"]["type"], "integer")
        self.assertEqual(operation["example"]["op"], "trim_item")
        guidance = handle_request(
            request("tools/call", {"name": "shotcut_capabilities", "arguments": {}})
        )["result"]["structuredContent"]["feature_guidance"]
        self.assertIn("duplicate_item", guidance["edit_primitives"])
        self.assertIn("render_status", guidance["progress"])

    def test_every_advertised_operation_has_a_complete_query_contract(self) -> None:
        self.assertEqual(set(OPERATION_EXAMPLES), set(OPERATION_CATALOG))
        for name in OPERATION_CATALOG:
            with self.subTest(operation=name):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "shotcut_capabilities",
                            "arguments": {"operation": name},
                        },
                    )
                )
                details = response["result"]["structuredContent"]["operations"][name]
                self.assertEqual(details["example"]["op"], name)
                self.assertFalse(details["schema"]["additionalProperties"])

    def test_every_advertised_operation_reaches_the_document_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in OPERATION_CATALOG:
                with self.subTest(operation=name):
                    document = ProjectDocument.new(
                        root / f"{name}.mlt",
                        width=1920,
                        height=1080,
                        fps_num=30,
                        fps_den=1,
                        title="Contract test",
                    )
                    try:
                        document.apply_operation({"op": name})
                    except ToolError as exc:
                        self.assertNotIn("Unknown operation", str(exc))

    def test_conflicts_return_structured_recovery_context(self) -> None:
        def conflict(_arguments: dict[str, object]) -> dict[str, object]:
            raise ConflictError(
                "stale",
                expected_revision="a" * 64,
                current_revision="b" * 64,
            )

        with patch.dict(HANDLERS, {"inspect_project": conflict}):
            response = handle_request(
                request(
                    "tools/call",
                    {"name": "inspect_project", "arguments": {"path": "project.mlt"}},
                )
            )
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["recommended_action"], "inspect_project"
        )
        self.assertEqual(result["structuredContent"]["current_revision"], "b" * 64)

    def test_small_preview_is_returned_as_image_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.png"
            preview.write_bytes(b"small-png")

            def rendered(_arguments: dict[str, object]) -> dict[str, object]:
                return {
                    "created": True,
                    "path": str(preview),
                    "frame": 0,
                    "size_bytes": preview.stat().st_size,
                    "managed_output": True,
                }

            with patch.dict(HANDLERS, {"render_preview": rendered}):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "render_preview",
                            "arguments": {"project_path": "project.mlt"},
                        },
                    )
                )
        content = response["result"]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image"])
        self.assertEqual(content[1]["mimeType"], "image/png")

    def test_user_selected_preview_path_is_not_read_back_into_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.png"
            preview.write_bytes(b"user-selected-output")

            def rendered(_arguments: dict[str, object]) -> dict[str, object]:
                return {
                    "created": True,
                    "path": str(preview),
                    "frame": 0,
                    "size_bytes": preview.stat().st_size,
                    "managed_output": False,
                }

            with patch.dict(HANDLERS, {"render_preview": rendered}):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "render_preview",
                            "arguments": {
                                "project_path": "project.mlt",
                                "output_path": str(preview),
                            },
                        },
                    )
                )
        self.assertEqual(
            [item["type"] for item in response["result"]["content"]], ["text"]
        )

    def test_file_writing_tools_are_conservatively_destructive(self) -> None:
        listed = handle_request(request("tools/list"))
        by_name = {tool["name"]: tool for tool in listed["result"]["tools"]}
        for name in ("create_project", "render_preview", "start_render"):
            self.assertTrue(by_name[name]["annotations"]["destructiveHint"])


if __name__ == "__main__":
    unittest.main()
