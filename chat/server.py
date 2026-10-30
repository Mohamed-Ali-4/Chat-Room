"""Multi-client chat server with public and private messages.

Run:  python -m chat.server [--host 0.0.0.0] [--port 5000]
"""

import argparse
import logging
import socket
import socketserver
import threading

from . import protocol

log = logging.getLogger("chat.server")


class ChatRoom:
    """Thread-safe registry of connected clients and message routing."""

    def __init__(self):
        self._clients: dict[str, "ClientHandler"] = {}
        self._lock = threading.Lock()

    def join(self, name: str, handler: "ClientHandler") -> str | None:
        """Register a client. Returns an error string if the name is unusable."""
        if not protocol.valid_nick(name):
            return "Invalid nickname: use 1-20 letters, digits, '_' or '-'."
        with self._lock:
            if self._find(name) is not None:
                return f"Nickname '{name}' is already taken."
            self._clients[name] = handler
        return None

    def leave(self, name: str) -> bool:
        with self._lock:
            return self._clients.pop(name, None) is not None

    def users(self) -> list[str]:
        with self._lock:
            return sorted(self._clients, key=str.lower)

    def broadcast(self, message: dict, exclude: str | None = None) -> None:
        with self._lock:
            targets = [h for n, h in self._clients.items() if n != exclude]
        for handler in targets:
            handler.send(message)

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


class ClientHandler(socketserver.StreamRequestHandler):
    server: "ChatServer"

    def setup(self):
        super().setup()
        self.name: str | None = None
        self._send_lock = threading.Lock()

    def send(self, message: dict) -> None:
        try:
            with self._send_lock:
                self.wfile.write(protocol.encode(message))
                self.wfile.flush()
        except (OSError, ValueError):
            pass  # the reader loop will notice the disconnect and clean up

    def handle(self):
        room = self.server.room
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
                if not self.dispatch(room, message):
                    break
        except (OSError, UnicodeDecodeError):
            pass
        finally:
            if self.name and room.leave(self.name):
                log.info("%s left", self.name)
                room.broadcast({"type": "system", "text": f"{self.name} left the chat"})
                room.broadcast({"type": "users", "users": room.users()})

    def dispatch(self, room: ChatRoom, message: dict) -> bool:
        """Handle one client message. Returns False to close the connection."""
        kind = message.get("type")

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
            log.info("%s joined from %s", name, self.client_address)
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


class ChatServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler=ClientHandler):
        self.room = ChatRoom()
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
    parser = argparse.ArgumentParser(description="Chat room server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (0.0.0.0 = all, 127.0.0.1 = this machine only)")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with ChatServer((args.host, args.port)) as server:
        log.info("Chat server listening on %s:%d (TCP)", args.host, args.port)
        if args.host == "0.0.0.0":
            log.info("Other computers on the network can connect with --host %s --port %d",
                     lan_address(), args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("Shutting down")
            server.room.broadcast({"type": "system", "text": "Server is shutting down."})


if __name__ == "__main__":
    main()
