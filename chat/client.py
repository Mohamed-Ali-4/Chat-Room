"""Networking core shared by the terminal and GUI clients, plus the terminal client.

Run:  python -m chat.client [--host 127.0.0.1] [--port 5000] [--name alice]
"""

import argparse
import socket
import sys
import threading
from typing import Callable

from . import protocol


class ChatConnection:
    """A connection to the chat server that delivers incoming messages to a callback."""

    def __init__(self, host: str, port: int, on_message: Callable[[dict], None],
                 on_disconnect: Callable[[], None] | None = None):
        self.sock = socket.create_connection((host, port))
        self._rfile = self.sock.makefile("rb")
        self._send_lock = threading.Lock()
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.name: str | None = None
        self.last_sender: str | None = None  # for /r replies
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def send(self, message: dict) -> None:
        with self._send_lock:
            self.sock.sendall(protocol.encode(message))

    def set_nick(self, name: str) -> None:
        self.send({"type": "nick", "name": name})

    def send_public(self, text: str) -> None:
        self.send({"type": "public", "text": text})

    def send_private(self, to: str, text: str) -> None:
        self.send({"type": "private", "to": to, "text": text})

    def close(self) -> None:
        try:
            self.send({"type": "quit"})
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def _reader(self) -> None:
        try:
            for raw in self._rfile:
                try:
                    message = protocol.decode(raw)
                except ValueError:
                    continue
                if message["type"] == "welcome":
                    self.name = message["name"]
                elif message["type"] == "private" and message.get("from") != self.name:
                    self.last_sender = message["from"]
                self.on_message(message)
        except (OSError, ValueError):
            pass
        finally:
            if self.on_disconnect:
                self.on_disconnect()


def run_terminal(host: str, port: int, name: str | None) -> None:
    joined = threading.Event()
    nick_rejected = threading.Event()
    closed = threading.Event()
    users_requested = threading.Event()  # roster updates are only shown on /users
    print_lock = threading.Lock()

    def show(text: str) -> None:
        with print_lock:
            print(text, flush=True)

    def on_message(message: dict) -> None:
        if message["type"] == "welcome":
            joined.set()
        elif message["type"] == "error" and not joined.is_set():
            nick_rejected.set()
        if message["type"] == "users":
            if not users_requested.is_set():
                return
            users_requested.clear()
        show(protocol.format_message(message, conn.name))

    def on_disconnect() -> None:
        closed.set()
        joined.set()
        nick_rejected.set()
        show("*** Disconnected from server.")

    try:
        conn = ChatConnection(host, port, on_message, on_disconnect)
    except OSError as exc:
        sys.exit(f"Could not connect to {host}:{port}: {exc}")

    try:
        while not joined.is_set():
            nick = name or input("Nickname: ").strip()
            name = None
            nick_rejected.clear()
            conn.set_nick(nick)
            while not (joined.is_set() or nick_rejected.is_set()):
                joined.wait(0.1)
        while not closed.is_set():
            line = input()
            try:
                message = protocol.parse_input(line, conn.last_sender)
            except protocol.CommandError as exc:
                show(f"!!! {exc}")
                continue
            if message is None:
                continue
            if message["type"] == "help":
                show(protocol.HELP_TEXT)
                continue
            if message["type"] == "quit":
                break
            if message["type"] == "users":
                users_requested.set()
            conn.send(message)
    except (EOFError, KeyboardInterrupt):
        pass
    except OSError:
        show("*** Connection lost.")
    finally:
        conn.on_disconnect = None
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chat room terminal client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--name", help="nickname (prompted if omitted)")
    args = parser.parse_args(argv)
    run_terminal(args.host, args.port, args.name)


if __name__ == "__main__":
    main()
