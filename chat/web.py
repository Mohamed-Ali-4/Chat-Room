"""HTTP + WebSocket front end, so any device with a browser can join the chat.

The browser loads the page over plain HTTP, then opens a WebSocket on /ws.
WebSocket (RFC 6455) is implemented here by hand with the standard library:

1. Handshake: the browser sends an HTTP GET with "Upgrade: websocket" and a
   random Sec-WebSocket-Key. We reply "101 Switching Protocols" with
   Sec-WebSocket-Accept = base64(SHA-1(key + GUID)). From then on the same
   TCP connection carries WebSocket frames instead of HTTP.
2. Framing: each frame has a small header (FIN bit, opcode, payload length,
   and for client->server frames a 4-byte XOR mask) followed by the payload.

Every text frame carries one JSON message of the protocol in protocol.py, and
web users share the same ChatRoom as terminal/GUI users, so they can send
private messages to each other.
"""

import base64
import hashlib
import http.server
import logging
import socketserver
import struct
import threading
from pathlib import Path

from . import protocol
from .server import ChatRoom, Session

log = logging.getLogger("chat.web")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # fixed by RFC 6455
STATIC_DIR = Path(__file__).with_name("static")

OP_CONTINUATION, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


class WebSocketClosed(Exception):
    pass


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    """Build one unmasked, unfragmented frame (server->client frames are never masked)."""
    header = bytes([0x80 | opcode])  # FIN=1
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < 1 << 16:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    return header + payload


def read_exact(rfile, count: int) -> bytes:
    data = rfile.read(count)
    if len(data) < count:
        raise WebSocketClosed()
    return data


def read_frame(rfile) -> tuple[bool, int, bytes]:
    """Read one frame. Returns (fin, opcode, unmasked payload)."""
    first, second = read_exact(rfile, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", read_exact(rfile, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", read_exact(rfile, 8))
    if length > protocol.MAX_LINE:
        raise WebSocketClosed()
    mask = read_exact(rfile, 4) if masked else None
    payload = read_exact(rfile, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class WebHandler(http.server.BaseHTTPRequestHandler):
    server: "WebServer"
    server_version = "ChatRoom/1.0"
    protocol_version = "HTTP/1.1"  # WebSocket upgrades require HTTP/1.1

    def log_message(self, format, *args):
        log.debug("%s %s", self.address_string(), format % args)

    def do_GET(self, head_only: bool = False):
        path = self.path.split("?", 1)[0]
        if path == "/ws" and not head_only:
            self.handle_websocket()
        elif path in ("/", "/index.html"):
            self.serve_bytes((STATIC_DIR / "index.html").read_bytes(),
                             "text/html; charset=utf-8", head_only)
        elif path == "/healthz":  # for hosting platforms' health checks
            self.serve_bytes(b"ok\n", "text/plain", head_only)
        else:
            self.send_error(404)

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def serve_bytes(self, body: bytes, content_type: str, head_only: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def client_ip(self):
        """The real client address, even behind a hosting platform's reverse proxy."""
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return (forwarded.split(",")[0].strip(), "proxy")
        return self.client_address

    # -- WebSocket -----------------------------------------------------------
    def handle_websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self.send_error(400, "Expected a WebSocket upgrade")
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True  # don't parse further HTTP requests on this socket

        self._send_lock = threading.Lock()
        session = Session(self.server.room, self.client_ip(), self.send_message, "WebSocket")
        try:
            for text in self.messages():
                try:
                    message = protocol.decode(text)
                except ValueError:
                    self.send_message({"type": "error", "text": "Malformed message."})
                    continue
                if not session.dispatch(message):
                    break
        except (WebSocketClosed, OSError, UnicodeDecodeError):
            pass
        finally:
            session.close()
            self.send_raw(encode_frame(b"", OP_CLOSE))

    def messages(self):
        """Yield complete text messages, answering pings and joining fragments."""
        parts: list[bytes] = []
        while True:
            fin, opcode, payload = read_frame(self.rfile)
            if opcode == OP_CLOSE:
                return
            if opcode == OP_PING:
                self.send_raw(encode_frame(payload, OP_PONG))
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY, OP_CONTINUATION):
                parts.append(payload)
                if fin:
                    yield b"".join(parts).decode(protocol.ENCODING)
                    parts = []

    def send_message(self, message: dict) -> None:
        self.send_raw(encode_frame(protocol.encode(message).rstrip(b"\n")))

    def send_raw(self, frame: bytes) -> None:
        try:
            with self._send_lock:
                self.wfile.write(frame)
                self.wfile.flush()
        except (OSError, ValueError):
            pass


class WebServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, room: ChatRoom):
        self.room = room
        super().__init__(address, WebHandler)
