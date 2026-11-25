"""In-memory store for files shared in the chat (photos, videos, documents...).

A file is uploaded with an HTTP POST and gets a random 128-bit id. The chat
message only carries that id; recipients download the bytes with an HTTP GET
on /files/<id>/<name>. The id is unguessable, so only people who received
the message (the room, or the one person in a private chat) know the link.
"""

import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_FILE_SIZE = 10 * 1024 * 1024    # per file
MAX_TOTAL_SIZE = 150 * 1024 * 1024  # whole store; the oldest files are dropped beyond this

# Types the browser may show inside the page. Everything else (HTML, SVG, scripts,
# office documents...) is served as a download, so an upload can't run code on our origin.
INLINE_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "video/mp4", "video/webm", "video/quicktime",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm", "audio/mp4", "audio/aac",
    "application/pdf",
}
_TYPE_RE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


@dataclass
class StoredFile:
    id: str
    name: str
    type: str
    data: bytes
    owner: str  # upload token of the session that sent it
    created: float = field(default_factory=time.time)

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def inline(self) -> bool:
        return self.type in INLINE_TYPES

    def meta(self) -> dict:
        """What a chat message carries about the file."""
        return {"id": self.id, "name": self.name, "size": self.size, "type": self.type,
                "url": f"/files/{self.id}/{self.name}"}


def clean_name(name: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f\"<>|?*:]", "", name).strip(" .")
    return name[:120] or "file"


def clean_type(content_type: str) -> str:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    return content_type if _TYPE_RE.match(content_type) else "application/octet-stream"


class FileStore:
    def __init__(self, max_total: int = MAX_TOTAL_SIZE):
        self._files: OrderedDict[str, StoredFile] = OrderedDict()
        self._total = 0
        self._max_total = max_total
        self._lock = threading.Lock()

    def add(self, data: bytes, name: str, content_type: str, owner: str) -> StoredFile:
        stored = StoredFile(secrets.token_urlsafe(16), clean_name(name), clean_type(content_type), data, owner)
        with self._lock:
            self._files[stored.id] = stored
            self._total += stored.size
            while self._total > self._max_total and len(self._files) > 1:
                _, old = self._files.popitem(last=False)
                self._total -= old.size
        return stored

    def get(self, file_id: str) -> StoredFile | None:
        with self._lock:
            return self._files.get(file_id)
