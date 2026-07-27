from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shotcut_mcp import (
    MLT_VERSION,
    MLT_VERSION_FAMILY,
    SHOTCUT_VERSION,
)
from shotcut_mcp import (
    render_jobs as render_jobs_module,
)
from shotcut_mcp.errors import ConflictError, RequestCancelled, ToolError
from shotcut_mcp.project import (
    EDIT_OPERATION_CONTRACTS,
    ProjectDocument,
    create_project,
)
from shotcut_mcp.protocol import cancellation_requested, schema_errors
from shotcut_mcp.server import (
    HANDLERS,
    SERVER_INSTRUCTIONS,
    ProtocolSession,
    handle_request,
    serve,
    write_message,
)
from shotcut_mcp.tools import OPERATION_CATALOG, OPERATION_EXAMPLES, TOOLS
from tests.path_assertions import assert_canonical_path, assert_canonical_paths


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


class SchemaValidationTests(unittest.TestCase):
    def test_object_errors_preserve_keyword_and_property_order(self) -> None:
        schema = {
            "type": "object",
            "minProperties": 3,
            "required": ["missing"],
            "propertyNames": {"type": "string", "pattern": "^[a-z]+$"},
            "properties": {
                "Bad name": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        }

        self.assertEqual(
            schema_errors({"Bad name": 0, "extra": "value"}, schema),
            [
                "$ must contain at least 3 properties.",
                "$.missing is required.",
                "$ property 'Bad name' does not match the required pattern.",
                "$.Bad name must be at least 1.",
                "$.extra is not allowed.",
            ],
        )

    def test_array_errors_preserve_bounds_before_item_errors(self) -> None:
        schema = {
            "type": "array",
            "minItems": 3,
            "maxItems": 1,
            "items": {"type": "integer", "minimum": 5},
        }

        self.assertEqual(
            schema_errors(["value", 4], schema, "$.items"),
            [
                "$.items must contain at least 3 items.",
                "$.items must contain at most 1 items.",
                "$.items[0] must be of type integer.",
                "$.items[1] must be at least 5.",
            ],
        )

    def test_string_and_numeric_keywords_keep_their_existing_messages(self) -> None:
        self.assertEqual(
            schema_errors(
                "x",
                {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 0,
                    "pattern": "^z",
                },
            ),
            [
                "$ must contain at least 2 characters.",
                "$ must contain at most 0 characters.",
                "$ does not match the required pattern.",
            ],
        )
        self.assertEqual(
            schema_errors(4, {"type": "integer", "minimum": 5, "maximum": 3}),
            ["$ must be at least 5.", "$ must be at most 3."],
        )

    def test_composition_errors_preserve_alternative_details(self) -> None:
        self.assertEqual(
            schema_errors(
                False,
                {
                    "anyOf": [
                        {"type": "integer"},
                        {"type": "string"},
                    ]
                },
                "$.choice",
            ),
            [
                "$.choice must match at least one schema in anyOf: "
                "$.choice must be of type integer. OR "
                "$.choice must be of type string."
            ],
        )
        self.assertEqual(
            schema_errors(
                4,
                {
                    "oneOf": [
                        {"type": "integer"},
                        {"type": "number"},
                    ]
                },
            ),
            ["$ matches 2 schemas in oneOf; exactly one is required."],
        )

    def test_type_union_keeps_boolean_distinct_from_integer(self) -> None:
        schema = {"type": ["integer", "null"]}

        self.assertEqual(schema_errors(None, schema), [])
        self.assertEqual(
            schema_errors(True, schema),
            ["$ must be of type ['integer', 'null']."],
        )


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
        self.assertEqual(response["error"]["data"]["error_code"], "invalid_arguments")
        self.assertTrue(response["error"]["data"]["recoverable"])
        self.assertEqual(
            response["error"]["data"]["recommended_action"],
            "correct_arguments_and_retry",
        )
        self.assertEqual(response["error"]["data"]["recommended_tool"], "probe_media")

    def test_edit_project_schema_requires_revision_or_explicit_force(self) -> None:
        base_arguments = {
            "project_path": "C:/video/project.mlt",
            "operations": [{"op": "set_notes", "notes": "Updated"}],
        }
        with patch.dict(
            HANDLERS, {"edit_project": lambda _arguments: {"accepted": True}}
        ):
            missing = handle_request(
                request(
                    "tools/call",
                    {"name": "edit_project", "arguments": base_arguments},
                )
            )
            force_false = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "edit_project",
                        "arguments": {**base_arguments, "force": False},
                    },
                )
            )
            revision = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "edit_project",
                        "arguments": {
                            **base_arguments,
                            "expected_revision": "a" * 64,
                        },
                    },
                )
            )
            forced = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "edit_project",
                        "arguments": {**base_arguments, "force": True},
                    },
                )
            )

        for response in (missing, force_false):
            self.assertEqual(response["error"]["code"], -32602)
            errors = " ".join(response["error"]["data"]["validationErrors"])
            self.assertIn("expected_revision", errors)
            self.assertIn("force", errors)
        self.assertFalse(revision["result"]["isError"])
        self.assertFalse(forced["result"]["isError"])

    def test_edit_tools_enforce_published_operation_contracts_before_execution(
        self,
    ) -> None:
        base_arguments = {
            "project_path": "C:/video/project.mlt",
            "expected_revision": "a" * 64,
        }
        invalid_operations = (
            {"op": "set_notes", "notse": "Updated"},
            {"op": "update_marker", "marker_id": "0"},
            {
                "op": "set_clip_speed",
                "track": "V1",
                "item_index": 0,
                "speed": 0,
            },
            {"op": "trim_item", "track": "V1", "item_index": 0, "delta": 0},
        )
        with patch.dict(
            HANDLERS,
            {
                "edit_project": lambda _arguments: {"accepted": True},
                "plan_project_edit": lambda _arguments: {"accepted": True},
            },
        ):
            for tool_name in ("edit_project", "plan_project_edit"):
                for operation in invalid_operations:
                    with self.subTest(tool=tool_name, operation=operation["op"]):
                        response = handle_request(
                            request(
                                "tools/call",
                                {
                                    "name": tool_name,
                                    "arguments": {
                                        **base_arguments,
                                        "operations": [operation],
                                    },
                                },
                            )
                        )
                        self.assertEqual(response["error"]["code"], -32602)
                        errors = " ".join(response["error"]["data"]["validationErrors"])
                        self.assertIn("operations[0]", errors)

            valid = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "edit_project",
                        "arguments": {
                            **base_arguments,
                            "operations": [{"op": "set_notes", "notes": "Updated"}],
                        },
                    },
                )
            )
        self.assertFalse(valid["result"]["isError"])

    def test_restore_schema_requires_revision_or_explicit_force(self) -> None:
        base_arguments = {
            "project_path": "C:/video/project.mlt",
            "backup_path": "C:/video/project.backup.mlt",
        }
        with patch.dict(
            HANDLERS,
            {"restore_project_backup": lambda _arguments: {"accepted": True}},
        ):
            missing = handle_request(
                request(
                    "tools/call",
                    {"name": "restore_project_backup", "arguments": base_arguments},
                )
            )
            force_false = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "restore_project_backup",
                        "arguments": {**base_arguments, "force": False},
                    },
                )
            )
            revision = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "restore_project_backup",
                        "arguments": {
                            **base_arguments,
                            "expected_revision": "a" * 64,
                        },
                    },
                )
            )
            forced = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "restore_project_backup",
                        "arguments": {**base_arguments, "force": True},
                    },
                )
            )

        self.assertEqual(missing["error"]["code"], -32602)
        self.assertEqual(force_false["error"]["code"], -32602)
        self.assertFalse(revision["result"]["isError"])
        self.assertFalse(forced["result"]["isError"])

    def test_start_render_schema_enforces_one_range_mode(self) -> None:
        base_arguments = {
            "project_path": "C:/video/project.mlt",
            "output_path": "C:/video/output.mp4",
        }
        invalid_variants = (
            {**base_arguments, "in_frame": 0},
            {**base_arguments, "out_frame": 30},
            {
                **base_arguments,
                "in_frame": 0,
                "out_frame": 30,
                "marker_id": "1",
            },
        )
        valid_variants = (
            base_arguments,
            {**base_arguments, "in_frame": 0, "out_frame": 30},
            {**base_arguments, "marker_id": "1"},
        )
        with patch.dict(HANDLERS, {"start_render": lambda _arguments: {"ok": True}}):
            for arguments in invalid_variants:
                with self.subTest(invalid=arguments):
                    response = handle_request(
                        request(
                            "tools/call",
                            {"name": "start_render", "arguments": arguments},
                        )
                    )
                    self.assertEqual(response["error"]["code"], -32602)
            for arguments in valid_variants:
                with self.subTest(valid=arguments):
                    response = handle_request(
                        request(
                            "tools/call",
                            {"name": "start_render", "arguments": arguments},
                        )
                    )
                    self.assertFalse(response["result"]["isError"])

    def test_start_render_schema_rejects_invalid_consumer_property_shapes(
        self,
    ) -> None:
        base_arguments = {
            "project_path": "C:/video/project.mlt",
            "output_path": "C:/video/output.mp4",
        }
        with patch.dict(HANDLERS, {"start_render": lambda _arguments: {"ok": True}}):
            invalid_value = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "start_render",
                        "arguments": {
                            **base_arguments,
                            "consumer_properties": {"vcodec": ["libx264"]},
                        },
                    },
                )
            )
            too_many = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "start_render",
                        "arguments": {
                            **base_arguments,
                            "consumer_properties": {
                                f"option_{index}": index for index in range(51)
                            },
                        },
                    },
                )
            )
            invalid_name = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "start_render",
                        "arguments": {
                            **base_arguments,
                            "consumer_properties": {"bad option": "value"},
                        },
                    },
                )
            )
            valid = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "start_render",
                        "arguments": {
                            **base_arguments,
                            "consumer_properties": {
                                "vcodec": "libx264",
                                "bf": 3,
                                "movflags": True,
                            },
                        },
                    },
                )
            )

        for response in (invalid_value, too_many, invalid_name):
            self.assertEqual(response["error"]["code"], -32602)
        self.assertFalse(valid["result"]["isError"])

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
        self.assertEqual(response["error"]["data"]["error_code"], "request_cancelled")
        self.assertTrue(response["error"]["data"]["recoverable"])
        self.assertEqual(
            response["error"]["data"]["recommended_action"],
            "retry_if_still_requested",
        )

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

    def test_stdio_server_never_writes_an_oversized_response(self) -> None:
        output = io.BytesIO()
        response = {
            "jsonrpc": "2.0",
            "id": 17,
            "result": {"payload": "x" * 4_000},
        }

        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "1024"}):
            write_message(response, output)

        encoded = output.getvalue()
        self.assertLessEqual(len(encoded), 1024)
        fallback = json.loads(encoded)
        self.assertEqual(fallback["id"], 17)
        self.assertEqual(fallback["error"]["code"], -32603)
        self.assertIn("response", fallback["error"]["message"].lower())

    def test_stdio_compacts_an_oversized_progress_notification(self) -> None:
        output = io.BytesIO()
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {
                "progressToken": "token",
                "progress": 1,
                "total": 2,
                "message": "x" * 4_000,
            },
        }

        with patch.dict(os.environ, {"SHOTCUT_MCP_MAX_MESSAGE_BYTES": "1024"}):
            write_message(notification, output)

        encoded = output.getvalue()
        self.assertLessEqual(len(encoded), 1024)
        compact = json.loads(encoded)
        self.assertEqual(compact["method"], "notifications/progress")
        self.assertNotIn("message", compact["params"])


class ProtocolNegotiationTests(unittest.TestCase):
    def test_structured_results_do_not_duplicate_the_payload_as_text(self) -> None:
        payload = {"payload": "x" * 10_000}
        session = ProtocolSession(protocol_version="2025-11-25")
        with patch.dict(
            "shotcut_mcp.server.HANDLERS",
            {"shotcut_status": lambda _arguments: payload},
        ):
            response = handle_request(
                request("tools/call", {"name": "shotcut_status", "arguments": {}}),
                session,
            )

        result = response["result"]
        self.assertEqual(result["structuredContent"], payload)
        self.assertLess(len(result["content"][0]["text"]), 512)
        self.assertNotIn("x" * 100, result["content"][0]["text"])

    def test_initialize_instructions_route_common_safe_workflows(self) -> None:
        response = handle_request(
            request("initialize", {"protocolVersion": "2025-11-25"})
        )
        instructions = response["result"]["instructions"]
        first_window = instructions[:512]
        self.assertEqual(instructions, SERVER_INSTRUCTIONS)
        for phrase in (
            "saved on disk",
            "inspect_project",
            "expected_revision",
            "shotcut_capabilities",
            "force",
            "overwrite",
        ):
            self.assertIn(phrase, first_window)
        for phrase in (
            "render_contact_sheet",
            "validate_project",
            "valid=True",
            "ready=True",
            "edit_project",
            "analyze_media_quality",
            "inclusive frames",
            "render_status",
            "export_marker_chapters",
            "avoid concurrent saves",
            "recommended_action",
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
        create_description = by_name["create_project"]["description"]
        self.assertIn("probe representative source media", create_description)
        self.assertIn("defaults as user intent", create_description)
        render_status = by_name["render_status"]["outputSchema"]["properties"]
        self.assertEqual(render_status["progress_percent"]["type"], ["number", "null"])
        self.assertEqual(render_status["log_tail"]["type"], ["string", "null"])
        inspect_schema = by_name["inspect_project"]["outputSchema"]
        self.assertIn("tracks", inspect_schema["oneOf"][0]["required"])
        track_schema = inspect_schema["properties"]["tracks"]["items"]
        self.assertEqual(track_schema["properties"]["track_id"]["type"], "string")
        self.assertEqual(
            track_schema["properties"]["items"]["items"]["properties"]["item_index"][
                "type"
            ],
            "integer",
        )
        self.assertEqual(
            inspect_schema["properties"]["filters"]["items"]["properties"]["filter_id"][
                "type"
            ],
            ["string", "null"],
        )
        marker_schema = inspect_schema["properties"]["markers"]["items"]
        self.assertIn(
            "exclusive",
            marker_schema["properties"]["end_frame"]["description"].lower(),
        )
        self.assertTrue(schema_errors([42], inspect_schema["properties"]["tracks"]))

        with tempfile.TemporaryDirectory() as directory:
            document = ProjectDocument.new(
                Path(directory) / "schema.mlt",
                width=1920,
                height=1080,
                fps_num=30,
                fps_den=1,
                title="Output schema",
            )
            self.assertEqual(schema_errors(document.snapshot(), inspect_schema), [])

    def test_revision_descriptions_match_each_tool_contract(self) -> None:
        by_name = {tool["name"]: tool for tool in TOOLS}
        plan = by_name["plan_project_edit"]["inputSchema"]["properties"][
            "expected_revision"
        ]["description"]
        edit = by_name["edit_project"]["inputSchema"]["properties"][
            "expected_revision"
        ]["description"]
        chapters = by_name["export_marker_chapters"]["inputSchema"]["properties"][
            "expected_revision"
        ]["description"]
        restore = by_name["restore_project_backup"]["inputSchema"]["properties"][
            "expected_revision"
        ]["description"]

        self.assertIn("required", plan.lower())
        self.assertIn("force is not supported", plan.lower())
        for description in (edit, restore):
            self.assertIn("unless force=true", description.lower())
        self.assertIn("optional", chapters.lower())
        self.assertNotIn("force", chapters.lower())

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
        focused = response["result"]["structuredContent"]
        self.assertEqual(
            set(focused),
            {"operations", "transaction_guarantees"},
        )
        operation = focused["operations"]["trim_item"]
        self.assertEqual(operation["schema"]["properties"]["delta"]["type"], "integer")
        self.assertEqual(
            operation["schema"]["properties"]["ripple_scope"]["enum"],
            ["track", "all_unlocked"],
        )
        ripple_description = operation["schema"]["properties"]["ripple_scope"][
            "description"
        ]
        self.assertIn("edge+delta", ripple_description)
        self.assertIn("ripple=true", ripple_description)
        self.assertIn("locked", ripple_description)
        self.assertEqual(operation["example"]["op"], "trim_item")
        speed_map = handle_request(
            request(
                "tools/call",
                {
                    "name": "shotcut_capabilities",
                    "arguments": {"operation": "set_clip_speed_map"},
                },
            )
        )["result"]["structuredContent"]["operations"]["set_clip_speed_map"]
        self.assertEqual(
            schema_errors(
                {
                    "op": "set_clip_speed_map",
                    "track": "V1",
                    "item_index": 0,
                    "keyframes": [
                        {"frame": 0, "speed": -1},
                        {"frame": 30, "speed": -2},
                    ],
                },
                speed_map["schema"],
            ),
            [],
        )
        keyframe_properties = speed_map["schema"]["properties"]["keyframes"]["items"][
            "properties"
        ]
        self.assertIn(
            "output-clip-relative", keyframe_properties["frame"]["description"]
        )
        self.assertIn("same sign", keyframe_properties["speed"]["description"])
        full = handle_request(
            request("tools/call", {"name": "shotcut_capabilities", "arguments": {}})
        )["result"]["structuredContent"]
        output_schema = next(
            tool["outputSchema"]
            for tool in TOOLS
            if tool["name"] == "shotcut_capabilities"
        )
        self.assertEqual(schema_errors(focused, output_schema), [])
        self.assertEqual(schema_errors(full, output_schema), [])
        guidance = full["feature_guidance"]
        self.assertIn("compatibility", full)
        self.assertIn("render_presets", full)
        self.assertIn("workflow", full)
        self.assertIn("duplicate_item", guidance["edit_primitives"])
        self.assertIn("set_clip_opacity", guidance["edit_primitives"])
        self.assertIn("render_status", guidance["progress"])

    def test_compatibility_and_new_projects_share_one_version_contract(self) -> None:
        full = handle_request(
            request("tools/call", {"name": "shotcut_capabilities", "arguments": {}})
        )["result"]["structuredContent"]
        self.assertEqual(
            full["compatibility"],
            {
                "shotcut": SHOTCUT_VERSION,
                "mlt": MLT_VERSION_FAMILY,
                "project_format": "MLT XML",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            document = ProjectDocument.new(
                Path(directory) / "compatibility.mlt",
                width=1920,
                height=1080,
                fps_num=30,
                fps_den=1,
                title="Compatibility",
            )
        self.assertEqual(document.root.get("version"), MLT_VERSION)
        self.assertEqual(
            document.root.get("title"), f"Shotcut version {SHOTCUT_VERSION}"
        )

    def test_advertised_item_selectors_and_aliases_match_runtime_contracts(
        self,
    ) -> None:
        self.assertEqual(set(OPERATION_CATALOG), set(EDIT_OPERATION_CONTRACTS))
        advertised_item_refs: set[str] = set()
        advertised_aliases: set[str] = set()
        for name in OPERATION_CATALOG:
            response = handle_request(
                request(
                    "tools/call",
                    {
                        "name": "shotcut_capabilities",
                        "arguments": {"operation": name},
                    },
                )
            )
            properties = response["result"]["structuredContent"]["operations"][name][
                "schema"
            ]["properties"]
            if "item_ref" in properties:
                advertised_item_refs.add(name)
            if "as" in properties:
                advertised_aliases.add(name)

        self.assertEqual(
            advertised_item_refs,
            {
                name
                for name, contract in EDIT_OPERATION_CONTRACTS.items()
                if contract.selector_field is not None
            },
        )
        self.assertEqual(
            advertised_aliases,
            {
                name
                for name, contract in EDIT_OPERATION_CONTRACTS.items()
                if contract.alias_target is not None
            },
        )

    def test_marker_operation_schema_explains_exclusive_end(self) -> None:
        response = handle_request(
            request(
                "tools/call",
                {
                    "name": "shotcut_capabilities",
                    "arguments": {"operation": "add_marker"},
                },
            )
        )
        description = response["result"]["structuredContent"]["operations"][
            "add_marker"
        ]["schema"]["properties"]["end_frame"]["description"].lower()
        self.assertIn("exclusive", description)
        self.assertIn("point marker", description)
        self.assertIn("greater than or equal", description)

    def test_focused_operation_schemas_express_local_runtime_constraints(self) -> None:
        cases = (
            ("update_marker", {"op": "update_marker", "marker_id": "0"}),
            (
                "set_clip_speed",
                {
                    "op": "set_clip_speed",
                    "track": "V1",
                    "item_index": 0,
                    "speed": 0,
                },
            ),
            (
                "trim_item",
                {"op": "trim_item", "track": "V1", "item_index": 0, "delta": 0},
            ),
            (
                "set_clip_opacity",
                {
                    "op": "set_clip_opacity",
                    "track": "V1",
                    "item_index": 0,
                    "opacity_keyframes": [{"frame": 0, "opacity": 1.1}],
                },
            ),
        )
        for operation_name, operation in cases:
            with self.subTest(operation=operation_name):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "shotcut_capabilities",
                            "arguments": {"operation": operation_name},
                        },
                    )
                )
                schema = response["result"]["structuredContent"]["operations"][
                    operation_name
                ]["schema"]
                self.assertTrue(schema_errors(operation, schema))

    def test_output_schemas_describe_stable_fields_and_nested_collections(self) -> None:
        def assert_nested_shapes(schema: object, path: str) -> None:
            if isinstance(schema, list):
                for index, child in enumerate(schema):
                    assert_nested_shapes(child, f"{path}[{index}]")
                return
            if not isinstance(schema, dict):
                return
            child_types = schema.get("type")
            types = child_types if isinstance(child_types, list) else [child_types]
            if "array" in types:
                self.assertIn("items", schema, path)
            if "object" in types:
                self.assertTrue(
                    isinstance(schema.get("properties"), dict)
                    or isinstance(schema.get("additionalProperties"), dict),
                    path,
                )
            for name, child in schema.items():
                assert_nested_shapes(child, f"{path}.{name}")

        error_payload = {
            "error": "recoverable failure",
            "error_type": "ToolError",
            "error_code": "tool_error",
            "recoverable": True,
            "recommended_action": "review_error_and_correct_request",
            "recommended_tool": None,
            "details": {},
        }
        for tool in TOOLS:
            with self.subTest(tool=tool["name"]):
                schema = tool["outputSchema"]
                properties = schema["properties"]
                self.assertTrue(properties)
                self.assertEqual(len(schema.get("oneOf", [])), 2)
                success_required = schema["oneOf"][0]["required"]
                error_required = schema["oneOf"][1]["required"]
                self.assertTrue(success_required)
                self.assertTrue(set(success_required).issubset(properties))
                self.assertIn("error_code", error_required)
                self.assertTrue(set(error_required).issubset(properties))
                assert_nested_shapes(schema, tool["name"])
                self.assertEqual(schema_errors(error_payload, schema), [])

    def test_validate_project_output_schema_matches_clean_result(self) -> None:
        by_name = {tool["name"]: tool for tool in TOOLS}
        schema = by_name["validate_project"]["outputSchema"]
        with tempfile.TemporaryDirectory() as directory:
            document = ProjectDocument.new(
                Path(directory) / "project.mlt",
                width=1920,
                height=1080,
                fps_num=30,
                fps_den=1,
                title="Output contract",
            )
            payload = {
                "project": document.snapshot(),
                "valid": True,
                "return_code": 0,
                "diagnostic": None,
                "ready": True,
                "checks": {
                    "resources": {
                        "status": "passed",
                        "checked_count": 0,
                        "missing_resources": [],
                    },
                    "mlt_services": {
                        "status": "passed",
                        "required": {"producer": ["color"]},
                        "missing": {},
                        "errors": {},
                    },
                },
            }

        self.assertEqual(schema_errors(payload, schema), [])
        self.assertNotIn("validator", schema["properties"])
        self.assertEqual(schema["properties"]["diagnostic"]["type"], ["string", "null"])
        self.assertIn(
            "first project frame", schema["properties"]["valid"]["description"]
        )
        self.assertIn("local resources", schema["properties"]["ready"]["description"])
        self.assertIn(
            "required MLT services", schema["properties"]["ready"]["description"]
        )

    def test_create_project_result_matches_its_published_output_schema(self) -> None:
        schema = next(
            tool["outputSchema"] for tool in TOOLS if tool["name"] == "create_project"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "shotcut_mcp.project.validate_project_file",
                return_value={
                    "valid": True,
                    "return_code": 0,
                    "diagnostic": None,
                },
            ),
        ):
            payload = create_project(
                {
                    "project_path": str(Path(directory) / "project.mlt"),
                    "tracks": [{"kind": "audio", "name": "Narration"}],
                }
            )

        self.assertIn("track_id", payload["operation_results"][0])
        self.assertEqual(schema_errors(payload, schema), [])

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

    def test_animate_clip_contract_uses_creative_values_not_mlt_properties(
        self,
    ) -> None:
        response = handle_request(
            request(
                "tools/call",
                {
                    "name": "shotcut_capabilities",
                    "arguments": {"operation": "animate_clip"},
                },
            )
        )
        details = response["result"]["structuredContent"]["operations"]["animate_clip"]
        properties = details["schema"]["properties"]

        self.assertIn("item_ref", properties)
        self.assertIn("keyframes", properties)
        self.assertNotIn("service", properties)
        self.assertNotIn("properties", properties)
        point = properties["keyframes"]["items"]["properties"]
        self.assertEqual(
            set(point),
            {
                "frame",
                "center_x",
                "center_y",
                "scale",
                "rotation_degrees",
                "opacity",
                "volume_db",
            },
        )

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
        payload = result["structuredContent"]
        self.assertEqual(payload["recommended_action"], "inspect_project")
        self.assertEqual(payload["recommended_tool"], "inspect_project")
        self.assertEqual(payload["error_code"], "project_revision_conflict")
        self.assertTrue(payload["recoverable"])
        self.assertEqual(payload["current_revision"], "b" * 64)
        schema = next(
            tool["outputSchema"] for tool in TOOLS if tool["name"] == "inspect_project"
        )
        self.assertEqual(schema_errors(payload, schema), [])

    def test_tool_errors_publish_stable_recovery_metadata(self) -> None:
        def failure(_arguments: dict[str, object]) -> dict[str, object]:
            raise ToolError(
                "Project not found: C:/missing/project.mlt",
                code="project_not_found",
                recommended_action="check_project_path_and_retry",
                details={"path": "C:/missing/project.mlt"},
            )

        with patch.dict(HANDLERS, {"inspect_project": failure}):
            response = handle_request(
                request(
                    "tools/call",
                    {"name": "inspect_project", "arguments": {"path": "project.mlt"}},
                )
            )

        result = response["result"]
        self.assertTrue(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(payload["error_code"], "project_not_found")
        self.assertEqual(payload["recommended_action"], "check_project_path_and_retry")
        self.assertIsNone(payload["recommended_tool"])
        self.assertEqual(payload["details"]["path"], "C:/missing/project.mlt")
        schema = next(
            tool["outputSchema"] for tool in TOOLS if tool["name"] == "inspect_project"
        )
        self.assertEqual(schema_errors(payload, schema), [])

    def test_tool_error_details_are_bounded_at_the_protocol_seam(self) -> None:
        def failure(_arguments: dict[str, object]) -> dict[str, object]:
            raise ToolError(
                "bounded",
                details={
                    "long": "x" * 4000,
                    **{f"field_{index}": index for index in range(64)},
                },
            )

        with patch.dict(HANDLERS, {"inspect_project": failure}):
            response = handle_request(
                request(
                    "tools/call",
                    {"name": "inspect_project", "arguments": {"path": "project.mlt"}},
                )
            )

        details = response["result"]["structuredContent"]["details"]
        self.assertLessEqual(len(details), 32)
        self.assertTrue(all(len(str(value)) <= 2000 for value in details.values()))

    def test_generic_tool_errors_keep_a_safe_recovery_fallback(self) -> None:
        def failure(_arguments: dict[str, object]) -> dict[str, object]:
            raise ToolError("Unsupported project structure.")

        with patch.dict(HANDLERS, {"inspect_project": failure}):
            response = handle_request(
                request(
                    "tools/call",
                    {"name": "inspect_project", "arguments": {"path": "project.mlt"}},
                )
            )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error_code"], "tool_error")
        self.assertTrue(payload["recoverable"])
        self.assertEqual(
            payload["recommended_action"], "review_error_and_correct_request"
        )
        self.assertEqual(payload["details"], {})

    def test_missing_project_error_routes_caller_without_message_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.mlt"
            response = handle_request(
                request(
                    "tools/call",
                    {"name": "inspect_project", "arguments": {"path": str(missing)}},
                )
            )

        payload = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["error_code"], "project_not_found")
        self.assertEqual(payload["recommended_action"], "check_project_path_and_retry")
        assert_canonical_path(self, payload["details"]["path"], missing)
        schema = next(
            tool["outputSchema"] for tool in TOOLS if tool["name"] == "inspect_project"
        )
        self.assertEqual(schema_errors(payload, schema), [])

    def test_unsupported_project_structure_recommends_restoring_or_resaving(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "not-mlt.mlt"
            project.write_text("<not_mlt/>", encoding="utf-8")
            response = handle_request(
                request(
                    "tools/call",
                    {"name": "inspect_project", "arguments": {"path": str(project)}},
                )
            )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error_code"], "unsupported_project_structure")
        self.assertEqual(payload["recommended_action"], "restore_or_resave_project")
        self.assertEqual(payload["recommended_tool"], "list_project_backups")
        self.assertEqual(payload["details"]["reason"], "unexpected_root")

    def test_stale_render_history_cursor_restarts_the_query(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(render_jobs_module, "JOB_DIR", Path(directory) / "jobs"),
        ):
            response = handle_request(
                request(
                    "tools/call",
                    {"name": "list_render_jobs", "arguments": {"cursor": "0" * 32}},
                )
            )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error_code"], "render_history_cursor_not_found")
        self.assertEqual(payload["recommended_action"], "restart_render_history_query")
        self.assertEqual(payload["recommended_tool"], "list_render_jobs")
        self.assertEqual(payload["details"]["cursor"], "0" * 32)

    def test_missing_media_stream_recommends_probe_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "clip.mp4"
            media.write_bytes(b"media")
            probe = {"format": {}, "streams": [{"index": 0, "codec_type": "video"}]}
            with patch("shotcut_mcp.media.probe_media_raw", return_value=probe):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "analyze_media_quality",
                            "arguments": {
                                "path": str(media),
                                "video_stream_index": 7,
                            },
                        },
                    )
                )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error_code"], "media_stream_not_found")
        self.assertEqual(payload["recommended_action"], "probe_media_and_choose_stream")
        self.assertEqual(payload["recommended_tool"], "probe_media")
        self.assertEqual(payload["details"]["available_stream_indices"], [0])

    def test_missing_search_root_identifies_the_invalid_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_root = Path(directory) / "missing"
            document = SimpleNamespace(snapshot=lambda: {"resources": []})
            with patch(
                "shotcut_mcp.project.ProjectDocument.load", return_value=document
            ):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "diagnose_missing_media",
                            "arguments": {
                                "project_path": str(Path(directory) / "project.mlt"),
                                "search_roots": [str(missing_root)],
                            },
                        },
                    )
                )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error_code"], "search_root_not_found")
        self.assertEqual(
            payload["recommended_action"], "choose_existing_search_roots_and_retry"
        )
        self.assertEqual(payload["recommended_tool"], "diagnose_missing_media")
        assert_canonical_paths(
            self, payload["details"]["invalid_roots"], [missing_root]
        )

    def test_ffmpeg_capability_failure_recommends_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "clip.mp4"
            ffmpeg = Path(directory) / "ffmpeg"
            media.write_bytes(b"media")
            ffmpeg.write_bytes(b"executable")
            probe = {"format": {}, "streams": [{"index": 0, "codec_type": "video"}]}
            failed = SimpleNamespace(returncode=1, stdout="", stderr="filter failure")
            with (
                patch("shotcut_mcp.media.probe_media_raw", return_value=probe),
                patch(
                    "shotcut_mcp.media.discover_executables",
                    return_value=SimpleNamespace(ffmpeg=ffmpeg),
                ),
                patch("shotcut_mcp.media.require_executable", return_value=ffmpeg),
                patch("shotcut_mcp.media.run_capture", return_value=failed),
            ):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "analyze_media_quality",
                            "arguments": {"path": str(media), "analyzers": ["black"]},
                        },
                    )
                )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error_code"], "ffmpeg_capability_query_failed")
        self.assertEqual(payload["recommended_action"], "run_compatibility_diagnostics")
        self.assertEqual(payload["recommended_tool"], "shotcut_doctor")
        self.assertEqual(payload["details"]["query"], "filters")

    def test_empty_timeline_reports_no_visual_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "empty.mlt"
            document = SimpleNamespace(snapshot=lambda: {"duration_frames": 0})
            with patch(
                "shotcut_mcp.project.ProjectDocument.load", return_value=document
            ):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "render_contact_sheet",
                            "arguments": {"project_path": str(project_path)},
                        },
                    )
                )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error_code"], "no_visual_frame")
        self.assertEqual(
            payload["recommended_action"], "inspect_project_and_add_timeline_content"
        )
        self.assertEqual(payload["recommended_tool"], "inspect_project")
        self.assertEqual(payload["details"]["reason"], "empty_timeline")

    def test_optional_visual_failure_keeps_diagnosis_and_recovery_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "missing-media.mlt"
            candidate_path = root / "audio-only.mp3"
            candidate_path.write_bytes(b"audio")
            document = SimpleNamespace(
                snapshot=lambda: {
                    "resources": [
                        {
                            "reference_id": "resource-1",
                            "resource": "missing.mp4",
                            "resolved_path": str(root / "missing.mp4"),
                            "exists": False,
                        }
                    ]
                }
            )
            candidate = {
                "candidate_id": "candidate-1",
                "path": str(candidate_path),
                "score": 60,
                "match": "basename",
                "verified": False,
                "size_bytes": candidate_path.stat().st_size,
                "media": None,
            }
            visual_failure = ToolError(
                "None of the candidates produced a visual frame.",
                code="no_visual_frame",
                recommended_action="probe_candidates_or_skip_visualization",
                recommended_tool="probe_media",
                details={"candidate_count": 1},
            )
            with (
                patch(
                    "shotcut_mcp.project.ProjectDocument.load", return_value=document
                ),
                patch("shotcut_mcp.missing_media._discover_files", return_value=[]),
                patch(
                    "shotcut_mcp.missing_media._rank_candidates",
                    return_value=[candidate],
                ),
                patch(
                    "shotcut_mcp.missing_media.render_media_contact_sheet",
                    side_effect=visual_failure,
                ),
            ):
                response = handle_request(
                    request(
                        "tools/call",
                        {
                            "name": "diagnose_missing_media",
                            "arguments": {
                                "project_path": str(project_path),
                                "search_roots": [str(root)],
                                "visual_output_path": str(root / "candidates.png"),
                            },
                        },
                    )
                )

        result = response["result"]
        self.assertFalse(result.get("isError", False))
        payload = result["structuredContent"]
        self.assertEqual(payload["missing_count"], 1)
        self.assertEqual(payload["visual"]["error_code"], "no_visual_frame")
        self.assertEqual(payload["visual"]["recommended_tool"], "probe_media")
        self.assertEqual(payload["visual"]["details"]["candidate_count"], 1)
        schema = next(
            tool["outputSchema"]
            for tool in TOOLS
            if tool["name"] == "diagnose_missing_media"
        )
        self.assertEqual(schema_errors(payload, schema), [])

    def test_invalid_persistent_render_state_is_not_retried(self) -> None:
        job_id = "a" * 32
        metadata = {
            "job_id": job_id,
            "status": "queued",
            "worker_pid": None,
            "started_at": 0,
            "output_transaction": None,
        }
        with patch("shotcut_mcp.render.read_job", return_value=metadata):
            response = handle_request(
                request(
                    "tools/call",
                    {"name": "render_status", "arguments": {"job_id": job_id}},
                )
            )

        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["error_code"], "invalid_persistent_render_state")
        self.assertFalse(payload["recoverable"])
        self.assertEqual(payload["recommended_action"], "report_issue")
        self.assertEqual(payload["details"]["field"], "output_transaction")

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
