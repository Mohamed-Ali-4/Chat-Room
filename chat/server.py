"""Multi-client chat server with public and private messages.

Run:  python -m chat.server [--host 0.0.0.0] [--port 5000] [--web-port 8000]

Port 5000 serves the terminal/GUI clients over raw TCP. Port 8000 serves the
web page and WebSocket clients (see web.py). Both share the same rooms.

Users are grouped into rooms identified by a short code (e.g. "K7M2QX").
Messages, private ones included, never leave the room they were sent in.
"""

import argparse
import logging
import os
import secrets
import socket
import socketserver
import threading

from . import protocol

log = logging.getLogger("chat.server")


class ChatRoom:
    """Thread-safe registry of the clients in one room, and message routing."""

    def __init__(self, code: str = protocol.LOBBY):
        self.code = code
        self._clients: dict[str, "Session"] = {}
        self._lock = threading.Lock()

    def join(self, name: str, session: "Session") -> str | None:
        """Register a client. Returns an error string if the name is unusable."""
        if not protocol.valid_nick(name):
            return "Invalid nickname: use 1-20 letters, digits, '_' or '-'."
        with self._lock:
            if self._find(name) is not None:
                return f"Nickname '{name}' is already taken."
            self._clients[name] = session
        return None

    def leave(self, name: str) -> bool:
        with self._lock:
            return self._clients.pop(name, None) is not None

    def is_empty(self) -> bool:
        with self._lock:
            return not self._clients

    def users(self) -> list[str]:
        with self._lock:
            return sorted(self._clients, key=str.lower)

    def broadcast(self, message: dict, exclude: str | None = None) -> None:
        with self._lock:
            targets = [s for n, s in self._clients.items() if n != exclude]
        for session in targets:
            session.send(message)

    def send_typing(self, sender: str, recipient: str | None) -> None:
        """Relay "sender is typing": to one user (private chat) or the whole room."""
        if recipient is None:
            self.broadcast({"type": "typing", "from": sender, "private": False}, exclude=sender)
            return
        with self._lock:
            name = self._find(recipient)
            target = self._clients.get(name) if name and name != sender else None
        if target is not None:
            target.send({"type": "typing", "from": sender, "private": True})

    def send_private(self, sender: str, recipient: str, text: str) -> str | None:
        """Deliver a private message. Returns an error string on failure."""
        with self._lock:
            name = self._find(recipient)
            target = self._clients.get(name) if name else None
            origin = self._clients.get(sender)
        if target is None:
            return f"No such user: {recipient}"
        if name == sender:
            return "You cannot send a private message to yourself."
        message = {"type": "private", "from": sender, "to": name, "text": text}
        log.info("[%s] PRIVATE %s -> %s (%d chars, unicast)", self.code, sender, name, len(text))
        target.send(message)
        if origin is not None:
            origin.send(message)  # echo so the sender sees what was delivered
        return None

    def _find(self, name: str) -> str | None:
        """Case-insensitive lookup; caller must hold the lock."""
        lowered = name.lower()
        for existing in self._clients:
            if existing.lower() == lowered:
                return existing
        return None


class Rooms:
    """All rooms on the server, looked up by code.

    The default LOBBY room always exists (it is where clients that don't ask
    for a room go). Other rooms are created on demand and deleted when the
    last person leaves.
    """

    def __init__(self):
        self._rooms: dict[str, ChatRoom] = {protocol.LOBBY: ChatRoom(protocol.LOBBY)}
        self._lock = threading.Lock()

    def get(self, code: str) -> ChatRoom | None:
        with self._lock:
            return self._rooms.get(code)

    def lobby(self) -> ChatRoom:
        return self._rooms[protocol.LOBBY]

    def count(self) -> int:
        with self._lock:
            return len(self._rooms)

    def join(self, name: str, session: "Session", code: str | None = None,
             create: bool = False, rejoin: bool = False) -> tuple[ChatRoom | None, str | None]:
        """Put a user in a room. Returns (room, None) or (None, error message).

        create=True makes a brand-new room with a random code.
        code=... joins that room; with rejoin=True the room is recreated if it
        vanished (e.g. a browser reconnecting after a server restart).
        Neither: the LOBBY.
        """
        with self._lock:
            if create:
                code = self._new_code()
                room = self._rooms[code] = ChatRoom(code)
            elif code:
                code = protocol.normalize_code(code)
                if not protocol.valid_code(code):
                    return None, "Invalid room code."
                room = self._rooms.get(code)
                if room is None:
                    if not rejoin:
                        return None, f"Room {code} doesn't exist. Check the code or create a new room."
                    room = self._rooms[code] = ChatRoom(code)
            else:
                room = self._rooms[protocol.LOBBY]
            error = room.join(name, session)
            if error:
                if room.code != protocol.LOBBY and room.is_empty():
                    del self._rooms[room.code]  # don't keep a room nobody managed to enter
                return None, error
            return room, None

    def leave(self, room: ChatRoom, name: str) -> bool:
        with self._lock:
            left = room.leave(name)
            if left:
                log.info("[%s] %s left", room.code, name)
            if room.code != protocol.LOBBY and room.is_empty():
                self._rooms.pop(room.code, None)
                log.info("[%s] room closed (empty)", room.code)
            return left

    def broadcast_all(self, message: dict) -> None:
        with self._lock:
            rooms = list(self._rooms.values())
        for room in rooms:
            room.broadcast(message)

    def _new_code(self) -> str:
        """Random code from an alphabet without look-alikes (0/O, 1/I/L); caller holds the lock."""
        while True:
            code = "".join(secrets.choice(protocol.CODE_ALPHABET) for _ in range(protocol.CODE_LENGTH))
            if code not in self._rooms:
                return code


class Session:
    """One logged-in (or logging-in) user, independent of the transport.

    The raw TCP handler and the WebSocket handler both create a Session and
    feed it decoded messages, so routing rules live in one place.
    """

    def __init__(self, rooms: Rooms, address, send, transport: str):
        self.rooms = rooms
        self.room: ChatRoom | None = None  # set once the user has joined a room
        self.address = address
        self.send = send  # callable(dict) that writes one message to this client
        self.transport = transport
        self.name: str | None = None

    def dispatch(self, message: dict) -> bool:
        """Handle one client message. Returns False to close the connection."""
        kind = message.get("type")

        if kind == "ping":  # keep-alive, so proxies don't close idle connections
            self.send({"type": "pong"})
            return True

        if self.name is None:
            if kind != "nick":
                self.send({"type": "error", "text": "Choose a nickname first."})
                return True
            name = str(message.get("name", "")).strip()
            room, error = self.rooms.join(
                name, self, code=str(message.get("room") or "").strip() or None,
                create=bool(message.get("create")), rejoin=bool(message.get("rejoin")))
            if error:
                self.send({"type": "error", "text": error})
                return True
            self.name, self.room = name, room
            log.info("[%s] %s joined from %s via %s", room.code, name, self.address, self.transport)
            self.send({"type": "welcome", "name": name, "room": room.code})
            room.broadcast({"type": "system", "text": f"{name} joined the chat"}, exclude=name)
            room.broadcast({"type": "users", "users": room.users()})
            return True

        room = self.room

        text = str(message.get("text", "")).strip()
        if kind == "public":
            if text:
                log.info("[%s] PUBLIC  %s -> everyone in the room (%d chars, broadcast)",
                         room.code, self.name, len(text))
                room.broadcast({"type": "public", "from": self.name, "text": text})
        elif kind == "private":
            recipient = str(message.get("to", "")).strip()
            if not recipient or not text:
                self.send({"type": "error", "text": "Usage: /msg <user> <message>"})
            else:
                error = room.send_private(self.name, recipient, text)
                if error:
                    self.send({"type": "error", "text": error})
        elif kind == "users":
            self.send({"type": "users", "users": room.users()})
        elif kind == "typing":  # not logged: it is sent every couple of seconds while typing
            recipient = str(message.get("to") or "").strip() or None
            room.send_typing(self.name, recipient)
        elif kind == "quit":
            return False
        elif kind == "nick":
            self.send({"type": "error", "text": "You already have a nickname."})
        else:
            self.send({"type": "error", "text": f"Unknown message type: {kind}"})
        return True

    def close(self) -> None:
        """Remove the user from the room and tell everyone else."""
        if self.name and self.room and self.rooms.leave(self.room, self.name):
            self.room.broadcast({"type": "system", "text": f"{self.name} left the chat"})
            self.room.broadcast({"type": "users", "users": self.room.users()})


class ClientHandler(socketserver.StreamRequestHandler):
    """Raw TCP client: one JSON message per line."""

    server: "ChatServer"

    def setup(self):
        super().setup()
        self._send_lock = threading.Lock()
        self.session = Session(self.server.rooms, self.client_address, self.send, "TCP")

    def send(self, message: dict) -> None:
        try:
            with self._send_lock:
                self.wfile.write(protocol.encode(message))
                self.wfile.flush()
        except (OSError, ValueError):
            pass  # the reader loop will notice the disconnect and clean up

    def handle(self):
        try:
            for raw in self.rfile:
                if len(raw) > protocol.MAX_LINE:
                    self.send({"type": "error", "text": "Message too long."})
                    continue
                try:
                    message = protocol.decode(raw)
                except ValueError:
                    self.send({"type": "error", "text": "Malformed message."})
                    continue
                if not self.session.dispatch(message):
                    break
        except (OSError, UnicodeDecodeError):
            pass
        finally:
            self.session.close()


class ChatServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler=ClientHandler, rooms: Rooms | None = None):
        self.rooms = rooms or Rooms()
        super().__init__(address, handler)


def lan_address() -> str:
    """Best guess at this machine's LAN IP, for telling other computers where to connect."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # connect() on a UDP socket sends no packets; it only picks the outgoing interface.
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def main(argv=None):
    from .web import WebServer  # imported here because web.py imports this module

    parser = argparse.ArgumentParser(description="Chat room server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (0.0.0.0 = all, 127.0.0.1 = this machine only)")
    parser.add_argument("--port", type=int, default=5000,
                        help="TCP port for terminal/GUI clients (0 disables it)")
    # Hosting platforms (Render, Railway, Heroku, Fly...) tell the app which port to serve on
    # through the PORT environment variable and expose only that one port publicly.
    parser.add_argument("--web-port", type=int, default=int(os.environ.get("PORT", 8000)),
                        help="HTTP/WebSocket port for browsers (0 disables the website; "
                             "defaults to $PORT when set)")
    args = parser.parse_args(argv)
    if not args.port and not args.web_port:
        parser.error("at least one of --port and --web-port must be enabled")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.port and args.port == args.web_port:
        log.warning("Port %d is used for the website, so the raw TCP listener is disabled",
                    args.port)
        args.port = 0

    rooms = Rooms()
    servers = []
    ip = lan_address() if args.host == "0.0.0.0" else args.host
    if args.port:
        servers.append(ChatServer((args.host, args.port), rooms=rooms))
        log.info("Chat server listening on %s:%d (TCP)", args.host, args.port)
        log.info("Terminal/GUI clients on other computers: --host %s --port %d", ip, args.port)
    if args.web_port:
        servers.append(WebServer((args.host, args.web_port), rooms))
        log.info("Website: open http://%s:%d in a browser on any device on the network",
                 ip, args.web_port)

    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        rooms.broadcast_all({"type": "system", "text": "Server is shutting down."})
    finally:
        for server in servers[1:]:
            server.shutdown()
        for server in servers:
            server.server_close()


if __name__ == "__main__":
    main()
