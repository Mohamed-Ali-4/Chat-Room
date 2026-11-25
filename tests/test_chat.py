import queue
import threading
import unittest

from chat import protocol
from chat.client import ChatConnection
from chat.server import ChatServer


class ParseInputTests(unittest.TestCase):
    def test_public(self):
        self.assertEqual(protocol.parse_input("hello"), {"type": "public", "text": "hello"})

    def test_private_commands(self):
        expected = {"type": "private", "to": "bob", "text": "hi there"}
        for line in ("/msg bob hi there", "/w bob hi there", "/PM bob  hi there", "@bob hi there"):
            self.assertEqual(protocol.parse_input(line), expected, line)

    def test_reply(self):
        self.assertEqual(protocol.parse_input("/r ok", last_sender="carol"),
                         {"type": "private", "to": "carol", "text": "ok"})
        with self.assertRaises(protocol.CommandError):
            protocol.parse_input("/r ok")

    def test_errors(self):
        for line in ("/msg", "/msg bob", "@bob", "/nope"):
            with self.assertRaises(protocol.CommandError, msg=line):
                protocol.parse_input(line)

    def test_blank_and_misc(self):
        self.assertIsNone(protocol.parse_input("   "))
        self.assertEqual(protocol.parse_input("/users"), {"type": "users"})
        self.assertEqual(protocol.parse_input("/help"), {"type": "help"})


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = ChatServer(("127.0.0.1", 0))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.clients = []

    def tearDown(self):
        for conn, _ in self.clients:
            conn.on_disconnect = None
            conn.close()
        self.server.shutdown()
        self.server.server_close()

    def connect(self, name, **join):
        inbox = queue.Queue()
        conn = ChatConnection("127.0.0.1", self.port, inbox.put)
        self.clients.append((conn, inbox))
        conn.set_nick(name, **join)
        conn.room = self.expect(inbox, "welcome")["room"]
        return conn, inbox

    def expect(self, inbox, kind, **fields):
        while True:
            message = inbox.get(timeout=2)
            if message["type"] == kind and all(message.get(k) == v for k, v in fields.items()):
                return message

    def assert_nothing(self, inbox, kind):
        try:
            while True:
                message = inbox.get(timeout=0.3)
                self.assertNotEqual(message["type"], kind, message)
        except queue.Empty:
            pass

    def test_private_message_only_reaches_recipient(self):
        alice, a_in = self.connect("alice")
        bob, b_in = self.connect("bob")
        carol, c_in = self.connect("carol")

        alice.send(protocol.parse_input("/msg Bob secret"))
        got = self.expect(b_in, "private")
        self.assertEqual(got, {"type": "private", "from": "alice", "to": "bob", "text": "secret"})
        self.expect(a_in, "private", to="bob")  # sender gets an echo
        self.assert_nothing(c_in, "private")
        self.assertEqual(bob.last_sender, "alice")

        bob.send(protocol.parse_input("/r got it", bob.last_sender))
        self.expect(a_in, "private", **{"from": "bob", "text": "got it"})

    def test_public_reaches_everyone(self):
        alice, a_in = self.connect("alice")
        _, b_in = self.connect("bob")
        alice.send_public("hi all")
        self.expect(a_in, "public", text="hi all")
        self.expect(b_in, "public", text="hi all")

    def test_errors(self):
        alice, a_in = self.connect("alice")
        alice.send_private("nobody", "hello")
        self.assertIn("No such user", self.expect(a_in, "error")["text"])
        alice.send_private("alice", "hello")
        self.expect(a_in, "error")

    def test_duplicate_nick_and_user_list(self):
        self.connect("alice")
        inbox = queue.Queue()
        conn = ChatConnection("127.0.0.1", self.port, inbox.put)
        self.clients.append((conn, inbox))
        conn.set_nick("ALICE")
        self.assertIn("taken", self.expect(inbox, "error")["text"])
        conn.set_nick("bob")
        self.expect(inbox, "welcome")
        conn.send({"type": "users"})
        self.expect(inbox, "users", users=["alice", "bob"])


    def test_rooms_are_isolated(self):
        alice, a_in = self.connect("alice", create=True)
        code = alice.room
        self.assertRegex(code, r"^[A-Z2-9]{6}$")
        bob, b_in = self.connect("bob", room=code.lower())  # codes are case-insensitive
        self.assertEqual(bob.room, code)
        eve, e_in = self.connect("eve")  # no code -> LOBBY
        self.assertEqual(eve.room, protocol.LOBBY)

        self.expect(a_in, "users", users=["alice", "bob"])  # eve is not listed
        alice.send_public("room only")
        self.expect(b_in, "public", text="room only")
        self.assert_nothing(e_in, "public")

        eve.send_private("alice", "hi")  # alice is in another room
        self.assertIn("No such user", self.expect(e_in, "error")["text"])

    def test_same_nick_in_different_rooms(self):
        self.connect("alice")
        other, _ = self.connect("alice", create=True)
        self.assertNotEqual(other.room, protocol.LOBBY)

    def test_unknown_room_and_cleanup(self):
        inbox = queue.Queue()
        conn = ChatConnection("127.0.0.1", self.port, inbox.put)
        self.clients.append((conn, inbox))
        conn.set_nick("alice", room="ZZZZZZ")
        self.assertIn("doesn't exist", self.expect(inbox, "error")["text"])

        rooms = self.server.rooms
        creator, _ = self.connect("bob", create=True)
        self.assertIsNotNone(rooms.get(creator.room))
        creator.close()
        for _ in range(50):
            if rooms.get(creator.room) is None:
                break
            threading.Event().wait(0.05)
        self.assertIsNone(rooms.get(creator.room))  # empty rooms are deleted
        self.assertIsNotNone(rooms.get(protocol.LOBBY))  # the LOBBY never is

    def test_rejoin_recreates_room(self):
        # A browser reconnecting after a server restart asks for its old room back.
        inbox = queue.Queue()
        conn = ChatConnection("127.0.0.1", self.port, inbox.put)
        self.clients.append((conn, inbox))
        conn.send({"type": "nick", "name": "alice", "room": "ABCD23", "rejoin": True})
        self.assertEqual(self.expect(inbox, "welcome")["room"], "ABCD23")


if __name__ == "__main__":
    unittest.main()
