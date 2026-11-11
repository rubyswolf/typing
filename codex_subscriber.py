from __future__ import annotations

import base64
import argparse
import json
import os
import select
import socket
import struct
import time
from pathlib import Path
from typing import Callable

from translator_api import read_websocket_frame


HOST = "127.0.0.1"
PORT = 8765


class BufferedConnection:
    def __init__(self, connection: socket.socket, buffered: bytes) -> None:
        self.connection = connection
        self.buffered = bytearray(buffered)

    def recv(self, length: int) -> bytes:
        if self.buffered:
            chunk = bytes(self.buffered[:length])
            del self.buffered[:length]
            return chunk
        return self.connection.recv(length)


def client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    mask = os.urandom(4)
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + mask + masked


def receive_json(connection: BufferedConnection) -> dict:
    while True:
        frame = read_websocket_frame(connection)
        if frame is None:
            raise ConnectionError("translator closed the connection")
        opcode, payload = frame
        if opcode == 0x1:
            return json.loads(payload)
        if opcode == 0x8:
            raise ConnectionError("translator closed the connection")
        if opcode == 0x9:
            connection.connection.sendall(client_frame(payload, opcode=0xA))


def linger_until_timeout_or_disconnect(connection: BufferedConnection, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        readable, _, _ = select.select([connection.connection], [], [], remaining)
        if not readable:
            return True
        try:
            frame = read_websocket_frame(connection)
        except (ConnectionError, OSError):
            return False
        if frame is None:
            return False
        opcode, payload = frame
        if opcode == 0x8:
            return False
        if opcode == 0x9:
            connection.connection.sendall(client_frame(payload, opcode=0xA))


def collect_message_until_disconnect(
    connection: BufferedConnection,
    event_sink: Callable[[dict], None] | None = None,
) -> str:
    output_chunks: list[str] = []
    while True:
        try:
            frame = read_websocket_frame(connection)
        except (ConnectionError, OSError):
            return "".join(output_chunks)
        if frame is None:
            return "".join(output_chunks)
        opcode, payload = frame
        if opcode == 0x8:
            return "".join(output_chunks)
        if opcode == 0x9:
            connection.connection.sendall(client_frame(payload, opcode=0xA))
            continue
        if opcode != 0x1:
            continue
        event = json.loads(payload)
        if event_sink is not None:
            event_sink(event)
        if event.get("type") != "output":
            continue
        text = str(event.get("text", ""))
        if text == "\b":
            if output_chunks:
                output_chunks.pop()
        elif text:
            output_chunks.append(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test an approved House API subscription.")
    parser.add_argument(
        "--linger",
        type=float,
        default=0,
        help="Seconds to remain connected after receiving a completed stroke.",
    )
    parser.add_argument(
        "--until-kicked",
        action="store_true",
        help="Collect translated output until disconnected from the tray.",
    )
    parser.add_argument(
        "--log-events",
        type=Path,
        help="Write every pushed event to this JSONL file until disconnected.",
    )
    args = parser.parse_args()
    connection = socket.create_connection((HOST, PORT), timeout=5)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        connection.sendall(
            (
                "GET /v1/events HTTP/1.1\r\n"
                f"Host: {HOST}:{PORT}\r\n"
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
        if b"101 Switching Protocols" not in headers:
            raise ConnectionError("WebSocket upgrade was rejected")

        connection.settimeout(None)
        stream = BufferedConnection(connection, buffered)
        required = receive_json(stream)
        if required.get("type") != "registration_required":
            raise ConnectionError("translator did not request registration")
        connection.sendall(client_frame(json.dumps({"type": "register", "name": "Codex"}).encode()))
        print("Codex registration sent; waiting for the Windows Allow/Deny response...", flush=True)

        response = receive_json(stream)
        if response.get("type") != "subscribed":
            raise PermissionError("Codex connection was not approved")
        if args.until_kicked:
            print(
                "Codex approved; collecting translated output until you kick it from the tray...",
                flush=True,
            )
            log_file = None
            event_sink = None
            if args.log_events is not None:
                args.log_events.parent.mkdir(parents=True, exist_ok=True)
                log_file = args.log_events.open("w", encoding="utf-8")
                log_file.write(
                    json.dumps(
                        {
                            "type": "session_started",
                            "client_timestamp": time.time(),
                            "subscription": response,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                log_file.flush()

                def write_event(event: dict) -> None:
                    assert log_file is not None
                    log_file.write(
                        json.dumps(
                            {
                                "client_timestamp": time.time(),
                                "event": event,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    log_file.flush()

                event_sink = write_event
                print(f"Logging full event stream to {args.log_events}", flush=True)
            try:
                message = collect_message_until_disconnect(stream, event_sink)
            finally:
                if log_file is not None:
                    log_file.write(
                        json.dumps({"type": "session_ended", "client_timestamp": time.time()})
                        + "\n"
                    )
                    log_file.close()
            print(f"Disconnected. Complete message received: {message!r}", flush=True)
            return

        print("Codex approved and subscribed; waiting for one completed House stroke...", flush=True)

        while True:
            event = receive_json(stream)
            if event.get("type") == "stroke_finished":
                print("Completed House stroke received.", flush=True)
                if args.linger > 0:
                    print(
                        f"Completed stroke received; remaining connected for {args.linger:g} seconds...",
                        flush=True,
                    )
                    if not linger_until_timeout_or_disconnect(stream, args.linger):
                        print("Disconnected by the translator; Codex is stopping.", flush=True)
                        return
                print("Closing Codex connection.", flush=True)
                break
        connection.sendall(client_frame(struct.pack("!H", 1000), opcode=0x8))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
