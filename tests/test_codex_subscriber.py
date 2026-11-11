from __future__ import annotations

import json
import socket
import struct
import time
import unittest

from codex_subscriber import (
    BufferedConnection,
    collect_message_until_disconnect,
    linger_until_timeout_or_disconnect,
)
from translator_api import websocket_frame


class CodexSubscriberTest(unittest.TestCase):
    def test_collects_complete_output_until_disconnect(self) -> None:
        subscriber, translator = socket.socketpair()
        try:
            for text in ("hello", " ", "wrong", "\b", "world"):
                translator.sendall(
                    websocket_frame(
                        json.dumps({"type": "output", "text": text}).encode()
                    )
                )
            translator.sendall(websocket_frame(struct.pack("!H", 4003), opcode=0x8))
            message = collect_message_until_disconnect(BufferedConnection(subscriber, b""))
            self.assertEqual(message, "hello world")
        finally:
            subscriber.close()
            translator.close()

    def test_linger_ends_immediately_when_translator_kicks_connection(self) -> None:
        subscriber, translator = socket.socketpair()
        try:
            translator.sendall(
                websocket_frame(struct.pack("!H", 4003) + b"disconnected from tray", opcode=0x8)
            )
            started = time.monotonic()
            connected = linger_until_timeout_or_disconnect(
                BufferedConnection(subscriber, b""), 60
            )
            self.assertFalse(connected)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            subscriber.close()
            translator.close()

    def test_linger_handles_an_abrupt_socket_reset_as_a_kick(self) -> None:
        subscriber, translator = socket.socketpair()
        translator.close()
        try:
            connected = linger_until_timeout_or_disconnect(
                BufferedConnection(subscriber, b""), 60
            )
            self.assertFalse(connected)
        finally:
            subscriber.close()


if __name__ == "__main__":
    unittest.main()
