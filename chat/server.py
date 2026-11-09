"""Multi-client chat server with public and private messages.

Run:  python -m chat.server [--host 0.0.0.0] [--port 5000] [--web-port 8000]

Port 5000 serves the terminal/GUI clients over raw TCP. Port 8000 serves the
web page and WebSocket clients (see web.py). Both share one chat room.
"""

import argparse
import logging
import os
import socket
import socketserver
import threading

from . import protocol

log = logging.getLogger("chat.server")


class ChatRoom:
    """Thread-safe registry of connected clients and message routing."""

    def __init__(self):
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

    def users(self) -> list[str]:
        with self._lock:
            return sorted(self._clients, key=str.lower)

    def broadcast(self, message: dict, exclude: str | None = None) -> None:
        with self._lock:
            targets = [s for n, s in self._clients.items() if n != exclude]
        for session in targets:
            session.send(message)

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
        log.info("PRIVATE %s -> %s (%d chars, unicast)", sender, name, len(text))
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


class Session:
    """One logged-in (or logging-in) user, independent of the transport.

    The raw TCP handler and the WebSocket handler both create a Session and
    feed it decoded messages, so routing rules live in one place.
    """

    def __init__(self, room: ChatRoom, address, send, transport: str):
        self.room = room
        self.address = address
        self.send = send  # callable(dict) that writes one message to this client
        self.transport = transport
        self.name: str | None = None

    def dispatch(self, message: dict) -> bool:
        """Handle one client message. Returns False to close the connection."""
        room = self.room
        kind = message.get("type")

        if kind == "ping":  # keep-alive, so proxies don't close idle connections
            self.send({"type": "pong"})
            return True

        if self.name is None:
            if kind != "nick":
                self.send({"type": "error", "text": "Choose a nickname first."})
                return True
            name = str(message.get("name", "")).strip()
            error = room.join(name, self)
            if error:
                self.send({"type": "error", "text": error})
                return True
            self.name = name
            log.info("%s joined from %s via %s", name, self.address, self.transport)
            self.send({"type": "welcome", "name": name})
            room.broadcast({"type": "system", "text": f"{name} joined the chat"}, exclude=name)
            room.broadcast({"type": "users", "users": room.users()})
            return True

        text = str(message.get("text", "")).strip()
        if kind == "public":
            if text:
                log.info("PUBLIC  %s -> everyone (%d chars, broadcast)", self.name, len(text))
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
        elif kind == "quit":
            return False
        elif kind == "nick":
            self.send({"type": "error", "text": "You already have a nickname."})
        else:
            self.send({"type": "error", "text": f"Unknown message type: {kind}"})
        return True

    def close(self) -> None:
        """Remove the user from the room and tell everyone else."""
        if self.name and self.room.leave(self.name):
            log.info("%s left", self.name)
            self.room.broadcast({"type": "system", "text": f"{self.name} left the chat"})
            self.room.broadcast({"type": "users", "users": self.room.users()})


class ClientHandler(socketserver.StreamRequestHandler):
    """Raw TCP client: one JSON message per line."""

    server: "ChatServer"

    def setup(self):
        super().setup()
        self._send_lock = threading.Lock()
        self.session = Session(self.server.room, self.client_address, self.send, "TCP")

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

    def __init__(self, address, handler=ClientHandler, room: ChatRoom | None = None):
        self.room = room or ChatRoom()
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

    room = ChatRoom()
    servers = []
    ip = lan_address() if args.host == "0.0.0.0" else args.host
    if args.port:
        servers.append(ChatServer((args.host, args.port), room=room))
        log.info("Chat server listening on %s:%d (TCP)", args.host, args.port)
        log.info("Terminal/GUI clients on other computers: --host %s --port %d", ip, args.port)
    if args.web_port:
        servers.append(WebServer((args.host, args.web_port), room))
        log.info("Website: open http://%s:%d in a browser on any device on the network",
                 ip, args.web_port)

    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        room.broadcast({"type": "system", "text": "Server is shutting down."})
    finally:
        for server in servers[1:]:
            server.shutdown()
        for server in servers:
            server.server_close()


if __name__ == "__main__":
    main()
