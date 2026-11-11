from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chord_translator import ChordTranslator
from translator_api import read_websocket_frame


ROOT = Path(__file__).resolve().parents[1]


class BufferedSocket:
    def __init__(self, connection: socket.socket, buffered: bytes) -> None:
        self.connection = connection
        self.buffered = bytearray(buffered)

    def recv(self, length: int) -> bytes:
        if self.buffered:
            chunk = bytes(self.buffered[:length])
            del self.buffered[:length]
            return chunk
        return self.connection.recv(length)

    def sendall(self, data: bytes) -> None:
        self.connection.sendall(data)

    def close(self) -> None:
        self.connection.close()


class TranslatorApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.translator = ChordTranslator(ROOT / "layouts" / "house_extended.json", api_port=0)
        self.translator.api_confirm_connection = lambda _name, _address: True  # type: ignore[method-assign]
        assert self.translator.api_server is not None
        self.port = self.translator.api_server.start()

    def tearDown(self) -> None:
        assert self.translator.api_server is not None
        self.translator.api_server.stop()

    def get_json(self, path: str, token: str = "") -> dict:
        request = Request(f"http://127.0.0.1:{self.port}{path}")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def post_json(self, path: str, payload: dict, token: str = "") -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def open_websocket(self) -> BufferedSocket:
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        key = base64.b64encode(b"house-api-test-key").decode("ascii")
        connection.sendall(
            (
                "GET /v1/events HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(connection.recv(4096))
        headers, buffered = bytes(response).split(b"\r\n\r\n", 1)
        self.assertIn(b"101 Switching Protocols", headers)
        return BufferedSocket(connection, buffered)

    @staticmethod
    def send_json(connection: BufferedSocket, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        else:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", len(data)))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        connection.sendall(bytes(header) + mask + masked)

    @staticmethod
    def receive_json(connection: BufferedSocket) -> dict:
        frame = read_websocket_frame(connection)
        if frame is None:
            raise AssertionError("WebSocket closed unexpectedly")
        opcode, payload = frame
        if opcode != 0x1:
            raise AssertionError(f"expected text frame, got opcode {opcode}")
        return json.loads(payload)

    def test_layout_is_logical_and_device_independent(self) -> None:
        response = self.get_json("/v1/layout")
        layout = response["layout"]
        self.assertEqual(layout["logical_keys"], ["A", "B", "C", "D", "1", "2", "3", "4", "L", "R"])
        self.assertNotIn("bindings", layout)

    def test_state_requires_an_approved_live_connection(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.get_json("/v1/state")
        self.assertEqual(caught.exception.code, 401)

    def test_null_browser_origin_is_rejected(self) -> None:
        request = Request(
            f"http://127.0.0.1:{self.port}/v1/layout",
            headers={"Origin": "null"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 403)

    def test_connection_can_be_kicked_and_token_is_invalidated(self) -> None:
        connection = self.open_websocket()
        try:
            self.assertEqual(self.receive_json(connection)["type"], "registration_required")
            self.send_json(connection, {"type": "register", "name": "Kick Test"})
            hello = self.receive_json(connection)
            connection_id = hello["connection"]["id"]
            token = hello["connection"]["token"]
            assert self.translator.api_server is not None
            self.assertTrue(self.translator.api_server.kick(connection_id))
            for _ in range(20):
                if not self.translator.api_server.connections():
                    break
                time.sleep(0.05)
            self.assertEqual(self.translator.api_server.connections(), [])
            with self.assertRaises(HTTPError) as caught:
                self.get_json("/v1/state", token)
            self.assertEqual(caught.exception.code, 401)
        finally:
            connection.close()

    def test_websocket_pushes_logical_chord_events(self) -> None:
        connection = self.open_websocket()
        try:
            required = self.receive_json(connection)
            self.assertEqual(required["type"], "registration_required")
            self.assertNotIn("state", required)
            self.send_json(connection, {"type": "register", "name": "House Trainer Test"})
            hello = self.receive_json(connection)
            self.assertEqual(hello["type"], "subscribed")
            self.assertEqual(hello["state"]["held"], [])
            token = hello["connection"]["token"]

            self.post_json("/v1/input", {"key": "A", "action": "down"}, token)
            self.post_json("/v1/input", {"key": "A", "action": "up"}, token)

            events = []
            while not any(event["type"] == "stroke_finished" for event in events):
                events.append(self.receive_json(connection))

            self.assertEqual(
                [event["action"] for event in events if event["type"] == "key"],
                ["down", "up"],
            )
            finished = next(event for event in events if event["type"] == "stroke_finished")
            self.assertEqual(finished["room"], "H")
            self.assertEqual(finished["keys"], ["A"])
            self.assertEqual(finished["emitted"], self.translator.layout["H"]["A"].lower())
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
