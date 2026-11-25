"""Wire protocol and command parsing shared by the server and the clients.

Every message is a single JSON object terminated by a newline ("JSON lines").

Client -> server:
    {"type": "nick",    "name": "alice"}                     join the default LOBBY room
    {"type": "nick",    "name": "alice", "create": true}     create a new room (random code)
    {"type": "nick",    "name": "alice", "room": "K7M2QX"}   join an existing room by code
    {"type": "public",  "text": "hello everyone"}
    {"type": "private", "to": "bob", "text": "hi bob"}
    {"type": "users"}
    {"type": "typing",  "to": "bob"}      "I'm typing" (omit "to" for the whole room)
    {"type": "ping"}                      keep-alive, answered with {"type": "pong"}
    {"type": "quit"}

Server -> client:
    {"type": "welcome", "name": "alice", "room": "K7M2QX"}
    {"type": "public",  "from": "alice", "text": "..."}
    {"type": "private", "from": "alice", "to": "bob", "text": "..."}
    {"type": "system",  "text": "bob joined the chat"}
    {"type": "users",   "users": ["alice", "bob"]}
    {"type": "typing",  "from": "alice", "private": true}
    {"type": "error",   "text": "..."}
"""

import json
import re

ENCODING = "utf-8"
MAX_LINE = 64 * 1024
NICK_RE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")

LOBBY = "LOBBY"  # default room for clients that don't give a code
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L look-alikes
CODE_LENGTH = 6
CODE_RE = re.compile(r"^[A-Z0-9]{4,12}$")

HELP_TEXT = """Commands:
  /msg <user> <message>   send a private message (aliases: /w, /pm, /whisper)
  @<user> <message>       shortcut for a private message
  /r <message>            reply privately to the last user who messaged you
  /users                  list connected users (alias: /who)
  /help                   show this help
  /quit                   leave the chat
Anything else is sent to everyone."""

PRIVATE_COMMANDS = {"/msg", "/w", "/pm", "/whisper", "/tell"}


class CommandError(ValueError):
    """Raised when user input looks like a command but is malformed."""


def encode(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode(ENCODING)


def decode(line: bytes | str) -> dict:
    if isinstance(line, bytes):
        line = line.decode(ENCODING)
    message = json.loads(line)
    if not isinstance(message, dict) or "type" not in message:
        raise ValueError("message must be a JSON object with a 'type'")
    return message


def valid_nick(name: str) -> bool:
    return bool(NICK_RE.match(name or ""))


def normalize_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "").replace("-", "")


def valid_code(code: str) -> bool:
    return bool(CODE_RE.match(code or ""))


def parse_input(line: str, last_sender: str | None = None) -> dict | None:
    """Turn a line typed by the user into a protocol message.

    Returns None for blank input and {"type": "help"} for /help (handled
    locally by the client). Raises CommandError for malformed commands.
    """
    line = line.strip()
    if not line:
        return None

    if line.startswith("@") and len(line) > 1:
        target, _, text = line[1:].partition(" ")
        return _private(target, text)

    if not line.startswith("/"):
        return {"type": "public", "text": line}

    command, _, rest = line.partition(" ")
    command = command.lower()
    rest = rest.strip()

    if command in PRIVATE_COMMANDS:
        target, _, text = rest.partition(" ")
        return _private(target, text)
    if command in ("/r", "/reply"):
        if not last_sender:
            raise CommandError("Nobody has sent you a private message yet.")
        return _private(last_sender, rest)
    if command in ("/users", "/who", "/list"):
        return {"type": "users"}
    if command in ("/quit", "/exit"):
        return {"type": "quit"}
    if command in ("/help", "/?"):
        return {"type": "help"}
    raise CommandError(f"Unknown command {command}. Type /help for a list.")


def _private(target: str, text: str) -> dict:
    target, text = target.strip(), text.strip()
    if not target or not text:
        raise CommandError("Usage: /msg <user> <message>")
    return {"type": "private", "to": target, "text": text}


def human_size(size: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def _with_attachment(message: dict) -> str:
    text = message.get("text", "")
    att = message.get("attachment")
    if not att:
        return text
    note = f"[attachment: {att['name']}, {human_size(att['size'])} - open the website to view]"
    return f"{text} {note}" if text else note


def format_message(message: dict, me: str | None = None) -> str:
    """Render a server message as a line of text for display."""
    kind = message.get("type")
    if kind == "public":
        return f"<{message['from']}> {_with_attachment(message)}"
    if kind == "private":
        if message["from"] == me:
            return f"[PM to {message['to']}] {_with_attachment(message)}"
        return f"[PM from {message['from']}] {_with_attachment(message)}"
    if kind == "users":
        return "Online: " + ", ".join(message["users"])
    if kind == "welcome":
        room = message.get("room", LOBBY)
        return f"*** Welcome, {message['name']}! You are in room {room}. Type /help for commands."
    if kind == "error":
        return f"!!! {message['text']}"
    return f"*** {message.get('text', '')}"
