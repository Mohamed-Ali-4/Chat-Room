import http.server
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from chat.preview import PreviewCache, parse_preview
from chat.server import ChatServer, Rooms
from chat.web import WebServer
from tests.test_web import RawWebSocket

PAGE = b"""<!doctype html><html><head><title>Fallback title</title>
<meta property="og:title" content="RFC 6455 &mdash; The WebSocket Protocol">
<meta property="og:description" content="Two-way communication over one TCP connection.">
<meta property="og:image" content="/logo.png"><meta property="og:site_name" content="IETF">
</head><body>hi</body></html>"""


class _PageHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        rooms = Rooms()
        self.tcp = ChatServer(("127.0.0.1", 0), rooms=rooms)
        self.web = WebServer(("127.0.0.1", 0), rooms, allow_private_previews=True)
        self.page = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        for server in (self.tcp, self.web, self.page):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.web.server_address[1]}"
        self.cleanup = []

    def tearDown(self):
        for close in self.cleanup:
            close()
        for server in (self.tcp, self.web, self.page):
            server.shutdown()
            server.server_close()

    def ws(self, name, **join):
        ws = RawWebSocket(self.web.server_address[1])
        self.cleanup.append(ws.close)
        ws.send({"type": "nick", "name": name, **join})
        welcome = ws.recv_json("welcome")
        ws.token, ws.room = welcome["token"], welcome["room"]
        return ws

    def upload(self, data, name, content_type, token):
        request = urllib.request.Request(f"{self.base}/upload", data=data, method="POST", headers={
            "Content-Type": content_type, "X-Filename": urllib.parse.quote(name), "X-Upload-Token": token})
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())

    def test_upload_requires_a_joined_session(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.upload(b"x", "a.txt", "text/plain", "not-a-token")
        self.assertEqual(caught.exception.code, 403)

    def test_upload_and_download(self):
        alice = self.ws("alice")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        status, meta = self.upload(png, "../../cat photo.png", "image/png", alice.token)
        self.assertEqual(status, 201)
        self.assertEqual((meta["name"], meta["size"], meta["type"]), ("cat photo.png", len(png), "image/png"))
        self.assertGreaterEqual(len(meta["id"]), 20)
        with urllib.request.urlopen(self.base + urllib.parse.quote(meta["url"])) as response:
            self.assertEqual(response.read(), png)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertTrue(response.headers["Content-Disposition"].startswith("inline"))

    def test_risky_types_are_downloads(self):
        alice = self.ws("alice")
        _, meta = self.upload(b"<script>alert(1)</script>", "x.html", "text/html", alice.token)
        with urllib.request.urlopen(self.base + meta["url"]) as response:
            self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
            self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("sandbox", response.headers["Content-Security-Policy"])

    def test_too_large_is_rejected_without_reading(self):
        alice = self.ws("alice")
        request = urllib.request.Request(f"{self.base}/upload", data=b"x", method="POST", headers={
            "X-Upload-Token": alice.token, "Content-Length": str(50 * 1024 * 1024)})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 413)

    def test_attachment_routing(self):
        alice = self.ws("alice", create=True)
        bob, carol = self.ws("bob", room=alice.room), self.ws("carol", room=alice.room)
        _, meta = self.upload(b"%PDF-1.4 report", "report.pdf", "application/pdf", alice.token)

        alice.send({"type": "private", "to": "bob", "text": "", "attachment": {"id": meta["id"]}})
        got = bob.recv_json("private")
        self.assertEqual(got["attachment"]["name"], "report.pdf")
        self.assertEqual(got["attachment"]["url"], meta["url"])

        alice.send({"type": "public", "text": "for everyone", "attachment": {"id": meta["id"], "name": "evil.exe"}})
        got = carol.recv_json("public")
        self.assertEqual(got["attachment"]["name"], "report.pdf")  # details come from the server, not the client

        # bob can't send a file someone else uploaded, or one that doesn't exist
        bob.send({"type": "public", "text": "", "attachment": {"id": meta["id"]}})
        self.assertIn("no longer available", bob.recv_json("error")["text"])

    def test_link_preview(self):
        alice = self.ws("alice")
        page = f"http://127.0.0.1:{self.page.server_address[1]}/moved"
        request = urllib.request.Request(f"{self.base}/preview?url={urllib.parse.quote(page)}",
                                         headers={"X-Upload-Token": alice.token})
        with urllib.request.urlopen(request) as response:
            preview = json.loads(response.read())
        self.assertEqual(preview["title"], "RFC 6455 \u2014 The WebSocket Protocol")
        self.assertEqual(preview["site"], "IETF")
        self.assertTrue(preview["image"].endswith("/logo.png"))
        self.assertEqual(preview["url"], page)

    def test_preview_blocks_private_addresses(self):
        # The real (non-test) setting must refuse to fetch internal addresses (SSRF).
        cache = PreviewCache(allow_private=False)
        for url in (f"http://127.0.0.1:{self.page.server_address[1]}/page", "http://localhost/",
                    "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd",
                    "ftp://example.com/"):
            self.assertIsNone(cache.get(url), url)

    def test_parse_preview_fallbacks(self):
        self.assertIsNone(parse_preview("https://x.test/", "<html><body>no meta</body></html>"))
        p = parse_preview("https://x.test/a", "<title> Plain   page </title><meta name=description content='d'>"
                                              "<meta property='og:image' content='javascript:alert(1)'>")
        self.assertEqual((p["title"], p["description"], p["image"], p["site"]), ("Plain page", "d", "", "x.test"))


if __name__ == "__main__":
    unittest.main()
