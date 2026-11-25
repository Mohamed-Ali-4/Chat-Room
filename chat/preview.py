"""Link previews: fetch a web page's title, description and image (Open Graph tags).

When a message contains a URL, the browser asks our server for a preview and
shows a card like WhatsApp does. Because the *server* makes the request, it
must not be usable to reach internal machines (SSRF), so:

* only http/https on the standard ports;
* the host name is resolved once and every address must be public
  (no 127.0.0.1, 10.x, 192.168.x, link-local, ...); we then connect to that
  exact address, so DNS can't change its answer between the check and the
  connection (DNS rebinding);
* redirects are followed by hand (at most 3), checking each new URL again;
* short timeout, only text/html, and at most 512 KB is read.
"""

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

TIMEOUT = 4
MAX_BYTES = 512 * 1024
CACHE_SECONDS = 600
USER_AGENT = "ChatRoomLinkPreview/1.0 (+https://github.com/Mohamed-Ali-4/Chat-Room)"


class PreviewError(Exception):
    pass


def _resolve(host: str, port: int, allow_private: bool) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PreviewError(f"cannot resolve {host}") from exc
    addresses = [info[4][0] for info in infos]
    if not allow_private:
        for address in addresses:
            if not ipaddress.ip_address(address).is_global:
                raise PreviewError(f"{host} is not a public address")
    return addresses[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip):
        super().__init__(host, port, timeout=TIMEOUT)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip):
        super().__init__(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # SNI + cert check by name


def _fetch_html(url: str, allow_private: bool) -> tuple[str, str]:
    """Return (final_url, html)."""
    for _ in range(4):  # the first request plus up to 3 redirects
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PreviewError("only http(s) links have previews")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in (80, 443) and not allow_private:
            raise PreviewError("non-standard port")
        ip = _resolve(parts.hostname, port, allow_private)
        conn_cls = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
        conn = conn_cls(parts.hostname, port, ip)
        try:
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html",
                                               "Accept-Language": "en"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308) and response.getheader("Location"):
                url = urljoin(url, response.getheader("Location"))
                continue
            if response.status != 200:
                raise PreviewError(f"HTTP {response.status}")
            content_type = response.getheader("Content-Type", "")
            if "html" not in content_type:
                raise PreviewError("not an HTML page")
            body = response.read(MAX_BYTES)
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
            try:
                return url, body.decode(charset, errors="replace")
            except LookupError:
                return url, body.decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            raise PreviewError(str(exc)) from exc
        finally:
            conn.close()
    raise PreviewError("too many redirects")


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and "content" in attrs and key not in self.meta:
                self.meta[key] = attrs["content"].strip()
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def parse_preview(url: str, html: str) -> dict | None:
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML: use whatever was parsed
        pass
    m = parser.meta
    title = m.get("og:title") or m.get("twitter:title") or parser.title.strip()
    description = m.get("og:description") or m.get("twitter:description") or m.get("description") or ""
    image = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image") or ""
    image = urljoin(url, image) if image else ""
    if image and urlsplit(image).scheme not in ("http", "https"):
        image = ""
    if not title and not description:
        return None
    site = m.get("og:site_name") or urlsplit(url).hostname or ""
    return {"url": url, "site": " ".join(site.split())[:80], "title": " ".join(title.split())[:200],
            "description": " ".join(description.split())[:300], "image": image[:1000]}


class PreviewCache:
    """Remembers previews (and failures) for a while so a busy room doesn't refetch them."""

    def __init__(self, allow_private: bool = False, size: int = 256):
        self.allow_private = allow_private
        self._cache: OrderedDict[str, tuple[float, dict | None]] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, url: str) -> dict | None:
        now = time.time()
        with self._lock:
            hit = self._cache.get(url)
            if hit and now - hit[0] < CACHE_SECONDS:
                return hit[1]
        try:
            final_url, html = _fetch_html(url, self.allow_private)
            result = parse_preview(final_url, html)
            if result:
                result["url"] = url  # the card links to what the person actually sent
        except PreviewError:
            result = None
        with self._lock:
            self._cache[url] = (now, result)
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
        return result
