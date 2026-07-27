from __future__ import annotations

import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from shotcut_mcp import server
from shotcut_mcp.protocol import report_progress


class ProgressProtocolTests(unittest.TestCase):
    def test_concurrent_requests_keep_progress_tokens_isolated(self) -> None:
        barrier = threading.Barrier(2)
        notifications: list[tuple[object, str | None]] = []

        def handler(arguments: dict[str, object]) -> dict[str, object]:
            barrier.wait(timeout=2)
            message = str(arguments["path"])
            report_progress(1, 1, message)
            return {"path": message}

        def call(token: str) -> dict[str, object] | None:
            return server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": token,
                    "method": "tools/call",
                    "params": {
                        "_meta": {"progressToken": token},
                        "name": "probe_media",
                        "arguments": {"path": token},
                    },
                },
                progress_callback=lambda progress_token, _progress, _total, message: (
                    notifications.append((progress_token, message))
                ),
            )

        with (
            patch.dict(server.HANDLERS, {"probe_media": handler}),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            responses = list(executor.map(call, ("request-a", "request-b")))
        self.assertTrue(
            all(response and "error" not in response for response in responses)
        )
        self.assertCountEqual(
            notifications,
            [("request-a", "request-a"), ("request-b", "request-b")],
        )

    def test_stdio_progress_is_token_scoped_and_revision_shaped(self) -> None:
        def handler(_arguments: dict[str, object]) -> dict[str, object]:
            report_progress(0, 2, "Starting")
            report_progress(0, 2, "Duplicate")
            report_progress(2, 2, "Complete")
            return {"ready": True}

        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "_meta": {"progressToken": "qc-2"},
                    "name": "shotcut_status",
                    "arguments": {},
                },
            },
        ]
        input_stream = io.BytesIO(
            ("\n".join(json.dumps(item) for item in messages) + "\n").encode()
        )
        output_stream = io.BytesIO()
        with patch.dict(server.HANDLERS, {"shotcut_status": handler}):
            server.serve(input_stream, output_stream)
        payloads = [
            json.loads(line) for line in output_stream.getvalue().decode().splitlines()
        ]
        progress = [
            item for item in payloads if item.get("method") == "notifications/progress"
        ]
        self.assertEqual([item["params"]["progress"] for item in progress], [0.0, 2.0])
        self.assertTrue(
            all(item["params"]["progressToken"] == "qc-2" for item in progress)
        )
        self.assertTrue(all("message" not in item["params"] for item in progress))
        self.assertEqual(payloads[-1]["id"], 2)

    def test_invalid_progress_token_is_rejected(self) -> None:
        for token in (True, None):
            with self.subTest(token=token):
                response = server.handle_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "_meta": {"progressToken": token},
                            "name": "shotcut_status",
                            "arguments": {},
                        },
                    }
                )
                assert response is not None
                self.assertEqual(response["error"]["code"], -32602)

    def test_current_protocol_progress_includes_the_bounded_message(self) -> None:
        notifications: list[tuple[object, float, float | None, str | None]] = []

        def handler(_arguments: dict[str, object]) -> dict[str, object]:
            report_progress(1, 1, "Complete")
            return {"ready": True}

        with patch.dict(server.HANDLERS, {"shotcut_status": handler}):
            response = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "_meta": {"progressToken": 7},
                        "name": "shotcut_status",
                        "arguments": {},
                    },
                },
                progress_callback=lambda token, progress, total, message: (
                    notifications.append((token, progress, total, message))
                ),
            )
        assert response is not None
        self.assertNotIn("error", response)
        self.assertEqual(notifications, [(7, 1.0, 1.0, "Complete")])


if __name__ == "__main__":
    unittest.main()
