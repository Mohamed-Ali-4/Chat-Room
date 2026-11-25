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
web users share the same rooms as terminal/GUI users, so they can send
private messages to each other.

Attachments travel over plain HTTP next to the WebSocket: POST /upload stores
the bytes and returns an id, the chat message carries only that id, and
recipients fetch the file with GET /files/<id>/<name>. GET /preview?url=...
returns a link preview (see preview.py). Both require the upload token the
server handed out in "welcome", so only people in a room can use them.
"""

import base64
import hashlib
import http.server
import json
import logging
import socketserver
import struct
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import protocol
from .files import MAX_FILE_SIZE
from .preview import PreviewCache
from .server import Rooms, Session

log = logging.getLogger("chat.web")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # fixed by RFC 6455
STATIC_DIR = Path(__file__).with_name("static")
# URL path -> (file in STATIC_DIR, content type). Only these files are served.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/qrcode.js": ("qrcode.js", "application/javascript; charset=utf-8"),  # MIT, Kazuhiko Arase
}

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
        elif path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            self.serve_bytes((STATIC_DIR / name).read_bytes(), content_type, head_only)
        elif path == "/healthz":  # for hosting platforms' health checks
            self.serve_bytes(b"ok\n", "text/plain", head_only)
        elif path.startswith("/files/"):
            self.serve_file(path, head_only)
        elif path == "/preview" and not head_only:
            self.serve_preview()
        else:
            self.send_error(404)

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_POST(self):
        if urlsplit(self.path).path != "/upload":
            self.close_connection = True
            self.send_error(404)
            return
        self.handle_upload()

    # -- attachments and link previews ---------------------------------------
    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def session(self) -> Session | None:
        return self.server.rooms.session_for_token(self.headers.get("X-Upload-Token", ""))

    def handle_upload(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        session = self.session()
        error = (None if session else (403, "Join a room first."))
        if not error and length < 0:
            error = (411, "Missing Content-Length.")
        if not error and length > MAX_FILE_SIZE:
            error = (413, f"Files can be at most {MAX_FILE_SIZE // (1024 * 1024)} MB.")
        if error:
            self.close_connection = True  # the body was not read, so the connection can't be reused
            self.send_json(error[0], {"error": error[1]})
            return
        data = self.rfile.read(length)
        if len(data) != length:
            self.close_connection = True
            return
        name = unquote(self.headers.get("X-Filename", ""))
        stored = self.server.rooms.files.add(data, name, self.headers.get("Content-Type", ""), session.token)
        log.info("[%s] UPLOAD  %s stored %s (%d bytes, %s)", session.room.code, session.name,
                 stored.name, stored.size, stored.type)
        self.send_json(201, stored.meta())

    def serve_file(self, path: str, head_only: bool) -> None:
        parts = path.split("/")  # ["", "files", id, name...]
        stored = self.server.rooms.files.get(parts[2]) if len(parts) >= 3 else None
        if stored is None:
            self.send_error(404, "File not found (files are kept only while the server runs)")
            return
        disposition = "inline" if stored.inline else "attachment"
        if parse_qs(urlsplit(self.path).query).get("download"):
            disposition = "attachment"
        self.send_response(200)
        self.send_header("Content-Type", stored.type if stored.inline else "application/octet-stream")
        self.send_header("Content-Length", str(stored.size))
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{quote(stored.name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        if stored.type != "application/pdf":  # Chrome won't show PDFs in a sandboxed document
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; media-src 'self'")
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        if not head_only:
            self.wfile.write(stored.data)

    def serve_preview(self) -> None:
        if self.session() is None:
            self.send_json(403, {"error": "Join a room first."})
            return
        url = (parse_qs(urlsplit(self.path).query).get("url") or [""])[0]
        if not url or len(url) > 2000:
            self.send_json(400, {"error": "Missing url."})
            return
        preview = self.server.previews.get(url)
        self.send_json(200, preview or {"url": url})

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
        session = Session(self.server.rooms, self.client_ip(), self.send_message, "WebSocket")
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

    def __init__(self, address, rooms: Rooms, allow_private_previews: bool = False):
        self.rooms = rooms
        # allow_private_previews is only for tests, which preview pages on 127.0.0.1
        self.previews = PreviewCache(allow_private=allow_private_previews)
        super().__init__(address, WebHandler)
