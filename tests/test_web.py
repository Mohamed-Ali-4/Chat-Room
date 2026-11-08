import base64
import json
import os
import queue
import socket
import struct
import threading
import unittest
import urllib.request

from chat.client import ChatConnection
from chat.server import ChatRoom, ChatServer
from chat.web import WebServer, accept_key


class RawWebSocket:
    """Minimal WebSocket client over a plain socket, to test the server byte by byte."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
        self.rfile = self.sock.makefile("rb")
        status = self.rfile.readline().decode()
        headers = {}
        for line in iter(self.rfile.readline, b"\r\n"):
            name, _, value = line.decode().partition(":")
            headers[name.strip().lower()] = value.strip()
        self.status, self.headers, self.key = status, headers, key

    def send(self, message, opcode=0x1):
        payload = json.dumps(message).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if len(payload) < 126:
            header = bytes([0x80 | opcode, 0x80 | len(payload)])
        else:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", len(payload))
        self.sock.sendall(header + mask + masked)

    def recv(self):
        first, second = self.rfile.read(2)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self.rfile.read(2))
        payload = self.rfile.read(length)
        return first & 0x0F, payload

    def recv_json(self, kind):
        while True:
            opcode, payload = self.recv()
            if opcode == 0x1:
                message = json.loads(payload)
                if message["type"] == kind:
                    return message

    def close(self):
        self.sock.close()


class WebTests(unittest.TestCase):
    def setUp(self):
        room = ChatRoom()
        self.tcp = ChatServer(("127.0.0.1", 0), room=room)
        self.web = WebServer(("127.0.0.1", 0), room)
        for server in (self.tcp, self.web):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.web_port = self.web.server_address[1]
        self.cleanup = []

    def tearDown(self):
        for close in self.cleanup:
            close()
        for server in (self.tcp, self.web):
            server.shutdown()
            server.server_close()

    def ws(self, name):
        ws = RawWebSocket(self.web_port)
        self.cleanup.append(ws.close)
        ws.send({"type": "nick", "name": name})
        ws.recv_json("welcome")
        return ws

    def test_accept_key_rfc_example(self):
        # Example from RFC 6455 section 1.3
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_serves_page(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.web_port}/") as response:
            self.assertIn(b"<title>Chat Room</title>", response.read())

    def test_handshake(self):
        ws = RawWebSocket(self.web_port)
        self.cleanup.append(ws.close)
        self.assertIn("101", ws.status)
        self.assertEqual(ws.headers["sec-websocket-accept"], accept_key(ws.key))

    def test_private_between_browsers(self):
        alice, bob, carol = self.ws("alice"), self.ws("bob"), self.ws("carol")
        alice.send({"type": "private", "to": "bob", "text": "secret"})
        self.assertEqual(bob.recv_json("private")["text"], "secret")
        self.assertEqual(alice.recv_json("private")["to"], "bob")
        carol.send({"type": "public", "text": "ping"})
        self.assertEqual(carol.recv_json("public")["text"], "ping")  # no PM queued before it

    def test_ping_gets_pong(self):
        alice = self.ws("alice")
        alice.send({}, opcode=0x9)
        while True:
            opcode, _ = alice.recv()
            if opcode == 0xA:
                break

    def test_browser_and_terminal_share_room(self):
        browser = self.ws("webuser")
        inbox = queue.Queue()
        terminal = ChatConnection("127.0.0.1", self.tcp.server_address[1], inbox.put)
        self.cleanup.append(terminal.close)
        terminal.set_nick("tcpuser")
        while inbox.get(timeout=2)["type"] != "welcome":
            pass

        browser.send({"type": "private", "to": "tcpuser", "text": "from the web"})
        while (message := inbox.get(timeout=2))["type"] != "private":
            pass
        self.assertEqual(message["from"], "webuser")

        terminal.send_private("webuser", "from the terminal")
        while (message := browser.recv_json("private"))["from"] != "tcpuser":
            pass  # skip the echo of our own message
        self.assertEqual(message["text"], "from the terminal")


if __name__ == "__main__":
    unittest.main()
