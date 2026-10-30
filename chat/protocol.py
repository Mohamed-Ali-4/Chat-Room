"""Wire protocol and command parsing shared by the server and the clients.

Every message is a single JSON object terminated by a newline ("JSON lines").

Client -> server:
    {"type": "nick",    "name": "alice"}
    {"type": "public",  "text": "hello everyone"}
    {"type": "private", "to": "bob", "text": "hi bob"}
    {"type": "users"}
    {"type": "quit"}

Server -> client:
    {"type": "welcome", "name": "alice"}
    {"type": "public",  "from": "alice", "text": "..."}
    {"type": "private", "from": "alice", "to": "bob", "text": "..."}
    {"type": "system",  "text": "bob joined the chat"}
    {"type": "users",   "users": ["alice", "bob"]}
    {"type": "error",   "text": "..."}
"""

import json
import re

ENCODING = "utf-8"
MAX_LINE = 64 * 1024
NICK_RE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")

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


def format_message(message: dict, me: str | None = None) -> str:
    """Render a server message as a line of text for display."""
    kind = message.get("type")
    if kind == "public":
        return f"<{message['from']}> {message['text']}"
    if kind == "private":
        if message["from"] == me:
            return f"[PM to {message['to']}] {message['text']}"
        return f"[PM from {message['from']}] {message['text']}"
    if kind == "users":
        return "Online: " + ", ".join(message["users"])
    if kind == "welcome":
        return f"*** Welcome, {message['name']}! Type /help for commands."
    if kind == "error":
        return f"!!! {message['text']}"
    return f"*** {message.get('text', '')}"
