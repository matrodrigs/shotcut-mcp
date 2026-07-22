from __future__ import annotations

import unittest

from shotcut_mcp.server import ProtocolSession, handle_request


def request(method: str, params: object = None, request_id: int = 1) -> dict[str, object]:
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


class ProtocolNegotiationTests(unittest.TestCase):
    def test_legacy_client_receives_only_legacy_tool_fields(self) -> None:
        session = ProtocolSession()
        handle_request(
            request("initialize", {"protocolVersion": "2024-11-05"}), session
        )

        listed = handle_request(request("tools/list"), session)
        tool = listed["result"]["tools"][0]
        self.assertNotIn("title", tool)
        self.assertNotIn("annotations", tool)

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

    def test_file_writing_tools_are_conservatively_destructive(self) -> None:
        listed = handle_request(request("tools/list"))
        by_name = {tool["name"]: tool for tool in listed["result"]["tools"]}
        for name in ("create_project", "render_preview", "start_render"):
            self.assertTrue(by_name[name]["annotations"]["destructiveHint"])


if __name__ == "__main__":
    unittest.main()
