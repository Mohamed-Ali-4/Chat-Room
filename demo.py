"""Self-contained demo: starts a server and three scripted clients in one process.

Run:  python demo.py

It shows that a private message reaches only its recipient, while a public
message reaches every client. Handy for presenting the project when you
can't open several terminals.
"""

import logging
import queue
import threading
import time

from chat import protocol
from chat.client import ChatConnection
from chat.server import ChatServer

STEPS = [
    ("alice", "Hello everyone!"),
    ("alice", "/msg bob Bob, this is a secret just for you."),
    ("bob", "/r Got it Alice, carol can't see this."),
    ("carol", "@alice Hi Alice, private hello from carol."),
    ("bob", "/msg dave are you there?"),  # unknown user -> error
    ("carol", "/users"),
]


def main():
    logging.basicConfig(level=logging.INFO, format="  [server] %(message)s")
    server = ChatServer(("127.0.0.1", 0))  # port 0 = let the OS pick a free port
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Server listening on 127.0.0.1:{port}\n")

    clients = {}
    for name in ("alice", "bob", "carol"):
        inbox = queue.Queue()
        conn = ChatConnection("127.0.0.1", port, inbox.put)
        conn.set_nick(name)
        clients[name] = (conn, inbox)
        time.sleep(0.1)

    def drain():
        time.sleep(0.3)
        for name, (conn, inbox) in clients.items():
            while not inbox.empty():
                message = inbox.get()
                if message["type"] == "users" and not show_users[0]:
                    continue
                print(f"  {name:>5} sees: {protocol.format_message(message, conn.name)}")

    show_users = [False]
    drain()
    for sender, line in STEPS:
        print(f"\n> {sender} types: {line}")
        conn, _ = clients[sender]
        message = protocol.parse_input(line, conn.last_sender)
        show_users[0] = message["type"] == "users"
        conn.send(message)
        drain()

    for conn, _ in clients.values():
        conn.close()
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
