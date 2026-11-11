from __future__ import annotations

import base64
import hashlib
import json
import queue
import secrets
import select
import socket
import struct
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import urlparse


API_VERSION = "1.1"


class TranslatorApi(Protocol):
    def api_layout(self) -> dict[str, Any]: ...

    def api_state(self) -> dict[str, Any]: ...

    def api_input(
        self, key: str, action: str, device: str, connection_id: str = ""
    ) -> dict[str, Any]: ...

    def api_confirm_connection(self, name: str, address: str) -> bool: ...

    def api_connection_closed(self, connection_id: str, name: str) -> None: ...

    def api_connections_changed(self) -> None: ...


class EventBroker:
    def __init__(self) -> None:
        self._next_id = 1
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    def publish(self, event_type: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            event = {
                "id": self._next_id,
                "type": event_type,
                "timestamp": time.time(),
                **payload,
            }
            self._next_id += 1
            for subscriber in tuple(self._subscribers):
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                        subscriber.put_nowait(
                            {
                                "id": event["id"],
                                "type": "overflow",
                                "timestamp": event["timestamp"],
                                "detail": "subscriber queue overflow; refresh /v1/state",
                            }
                        )
                    except (queue.Empty, queue.Full):
                        pass
            return event

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=512)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def local_origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True
    if origin == "null":
        return False
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def websocket_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def recv_exact(connection, length: int) -> bytes:
    payload = bytearray()
    while len(payload) < length:
        chunk = connection.recv(length - len(payload))
        if not chunk:
            raise ConnectionError("WebSocket closed during frame")
        payload.extend(chunk)
    return bytes(payload)


def read_websocket_frame(connection) -> tuple[int, bytes] | None:
    first = connection.recv(1)
    if not first:
        return None
    header = first + recv_exact(connection, 1)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(connection, 8))[0]
    mask = recv_exact(connection, 4) if masked else b""
    payload = bytearray(recv_exact(connection, length))
    if masked:
        payload = bytearray(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, bytes(payload)


class TranslatorApiServer:
    def __init__(
        self,
        translator: TranslatorApi,
        events: EventBroker,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.translator = translator
        self.events = events
        self.host = host
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._connections_lock = threading.Lock()
        self._connections: dict[str, dict[str, Any]] = {}

    def connections(self) -> list[dict[str, Any]]:
        with self._connections_lock:
            return [
                {
                    "id": connection_id,
                    "name": connection["name"],
                    "address": connection["address"],
                    "connected_at": connection["connected_at"],
                }
                for connection_id, connection in self._connections.items()
            ]

    def register_connection(self, name: str, address: str, connection: socket.socket) -> dict[str, Any]:
        entry = {
            "id": secrets.token_urlsafe(12),
            "token": secrets.token_urlsafe(32),
            "name": name,
            "address": address,
            "connected_at": time.time(),
            "socket": connection,
        }
        with self._connections_lock:
            self._connections[entry["id"]] = entry
        self.translator.api_connections_changed()
        return entry

    def remove_connection(self, connection_id: str) -> None:
        with self._connections_lock:
            entry = self._connections.pop(connection_id, None)
        if entry is not None:
            self.translator.api_connection_closed(connection_id, str(entry["name"]))
            self.translator.api_connections_changed()

    def authenticated_connection(self, authorization: str | None) -> dict[str, Any] | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:].strip()
        with self._connections_lock:
            for connection in self._connections.values():
                if secrets.compare_digest(str(connection["token"]), token):
                    return connection
        return None

    def kick(self, connection_id: str) -> bool:
        with self._connections_lock:
            entry = self._connections.get(connection_id)
        if entry is None:
            return False
        self.remove_connection(connection_id)
        connection = entry["socket"]
        try:
            reason = struct.pack("!H", 4003) + b"disconnected from tray"
            connection.sendall(websocket_frame(reason, opcode=0x8))
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        return True

    def start(self) -> int:
        if self.httpd is not None:
            return self.port
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "HouseTranslatorAPI/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def send_json(self, status: HTTPStatus, payload: Any) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                origin = self.headers.get("Origin")
                if origin and local_origin_allowed(origin):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(body)

            def trusted_origin(self) -> bool:
                if local_origin_allowed(self.headers.get("Origin")):
                    return True
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "local_origin_required"})
                return False

            def require_connection(self) -> dict[str, Any] | None:
                connection = outer.authenticated_connection(self.headers.get("Authorization"))
                if connection is None:
                    self.send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {
                            "error": "approved_connection_required",
                            "detail": "Register and receive approval through /v1/events first.",
                        },
                    )
                return connection

            def do_GET(self) -> None:
                if not self.trusted_origin():
                    return
                parsed = urlparse(self.path)
                try:
                    if parsed.path in {"/", "/v1"}:
                        self.send_json(
                            HTTPStatus.OK,
                            {
                                "name": "House Translator API",
                                "version": API_VERSION,
                                "endpoints": {
                                    "layout": "/v1/layout",
                                    "state": "/v1/state",
                                    "events": "ws://127.0.0.1:{port}/v1/events".format(port=outer.port),
                                    "input": "POST /v1/input",
                                },
                                "authentication": "WebSocket registration and explicit user approval",
                            },
                        )
                        return
                    if parsed.path == "/v1/layout":
                        self.send_json(HTTPStatus.OK, outer.translator.api_layout())
                        return
                    if parsed.path == "/v1/state":
                        if self.require_connection() is None:
                            return
                        self.send_json(HTTPStatus.OK, outer.translator.api_state())
                        return
                    if parsed.path == "/v1/events":
                        self.serve_events_websocket()
                        return
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                except (TypeError, ValueError) as error:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(error)})
                except Exception as error:
                    self.send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "internal_error", "detail": str(error)},
                    )

            def serve_events_websocket(self) -> None:
                if self.headers.get("Upgrade", "").lower() != "websocket":
                    self.send_json(
                        HTTPStatus.UPGRADE_REQUIRED,
                        {"error": "websocket_required", "endpoint": "/v1/events"},
                    )
                    return
                key = self.headers.get("Sec-WebSocket-Key", "")
                if not key:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_websocket_key"})
                    return
                accept = base64.b64encode(
                    hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
                ).decode("ascii")
                self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()

                subscriber: queue.Queue[dict[str, Any]] | None = None
                registered: dict[str, Any] | None = None
                last_ping = time.monotonic()
                try:
                    registration_required = {
                        "id": 0,
                        "type": "registration_required",
                        "timestamp": time.time(),
                        "api_version": API_VERSION,
                        "name_max_length": 64,
                    }
                    self.connection.sendall(
                        websocket_frame(json.dumps(registration_required).encode("utf-8"))
                    )
                    self.connection.settimeout(15.0)
                    frame = read_websocket_frame(self.connection)
                    if frame is None or frame[0] != 0x1:
                        raise ValueError("a text registration message is required")
                    request = json.loads(frame[1].decode("utf-8"))
                    if request.get("type") != "register":
                        raise ValueError("first message must have type 'register'")
                    name = " ".join(str(request.get("name", "")).split())
                    if not name or len(name) > 64 or not name.isprintable():
                        raise ValueError("name must be 1 to 64 printable characters")
                    address = str(self.client_address[0])
                    if not outer.translator.api_confirm_connection(name, address):
                        denied = {
                            "id": 0,
                            "type": "registration_denied",
                            "timestamp": time.time(),
                        }
                        self.connection.sendall(websocket_frame(json.dumps(denied).encode("utf-8")))
                        self.connection.sendall(
                            websocket_frame(struct.pack("!H", 4001) + b"not approved", opcode=0x8)
                        )
                        return

                    registered = outer.register_connection(name, address, self.connection)
                    subscriber = outer.events.subscribe()
                    self.connection.settimeout(None)
                    subscribed = {
                        "id": 0,
                        "type": "subscribed",
                        "timestamp": time.time(),
                        "api_version": API_VERSION,
                        "connection": {
                            "id": registered["id"],
                            "name": registered["name"],
                            "token": registered["token"],
                        },
                        "state": outer.translator.api_state(),
                    }
                    self.connection.sendall(websocket_frame(json.dumps(subscribed).encode("utf-8")))
                    while outer.httpd is not None:
                        try:
                            assert subscriber is not None
                            event = subscriber.get(timeout=0.25)
                            self.connection.sendall(
                                websocket_frame(json.dumps(event, separators=(",", ":")).encode("utf-8"))
                            )
                        except queue.Empty:
                            pass

                        readable, _, _ = select.select([self.connection], [], [], 0)
                        if readable:
                            frame = read_websocket_frame(self.connection)
                            if frame is None:
                                break
                            opcode, payload = frame
                            if opcode == 0x8:
                                self.connection.sendall(websocket_frame(payload, opcode=0x8))
                                break
                            if opcode == 0x9:
                                self.connection.sendall(websocket_frame(payload, opcode=0xA))

                        if time.monotonic() - last_ping >= 15.0:
                            self.connection.sendall(websocket_frame(b"house", opcode=0x9))
                            last_ping = time.monotonic()
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                    try:
                        failed = {
                            "id": 0,
                            "type": "registration_error",
                            "timestamp": time.time(),
                            "detail": str(error),
                        }
                        self.connection.sendall(websocket_frame(json.dumps(failed).encode("utf-8")))
                        self.connection.sendall(
                            websocket_frame(struct.pack("!H", 4002) + b"invalid registration", opcode=0x8)
                        )
                    except OSError:
                        pass
                except (BrokenPipeError, ConnectionError, ConnectionResetError, OSError):
                    pass
                finally:
                    if subscriber is not None:
                        outer.events.unsubscribe(subscriber)
                    if registered is not None:
                        outer.remove_connection(str(registered["id"]))
                    self.close_connection = True

            def do_POST(self) -> None:
                if not self.trusted_origin():
                    return
                if urlparse(self.path).path != "/v1/input":
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                connection = self.require_connection()
                if connection is None:
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 8192:
                        raise ValueError("request body must be between 1 and 8192 bytes")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = outer.translator.api_input(
                        str(body.get("key", "")),
                        str(body.get("action", "")),
                        str(connection["name"]),
                        str(connection["id"]),
                    )
                    self.send_json(HTTPStatus.OK, result)
                except (json.JSONDecodeError, TypeError, ValueError) as error:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(error)})
                except RuntimeError as error:
                    self.send_json(HTTPStatus.CONFLICT, {"error": "conflict", "detail": str(error)})
                except Exception as error:
                    self.send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "internal_error", "detail": str(error)},
                    )

            def do_OPTIONS(self) -> None:
                if not self.trusted_origin():
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                origin = self.headers.get("Origin")
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
                self.end_headers()

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="translator-api", daemon=True)
        self.thread.start()
        return self.port

    def stop(self) -> None:
        httpd = self.httpd
        self.httpd = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
