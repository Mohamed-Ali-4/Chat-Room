# Chat-Room

A multi-client TCP chat room in Python (standard library only) with **public and private messages**, a terminal client and a Tkinter GUI client.

## Running

Requires Python 3.10+.

```bash
python -m chat.server --port 5000      # listens on all interfaces; prints the LAN IP to share
```

```bash
python -m chat.client --port 5000 --name alice     # terminal client
```

```bash
python -m chat.gui_client --port 5000              # GUI client (needs tkinter)
```

To connect from another computer on the same network, pass the server's IP: `python -m chat.client --host 192.168.x.x --name bob`. If that fails, allow TCP port 5000 through the firewall.

To see the whole thing in a single terminal, which is handy for a presentation:

```bash
python demo.py
```

The full project report (networking concepts, protocol, diagrams, test results, viva questions) is in [`docs/REPORT.md`](docs/REPORT.md).

## Sending private messages

| Command | Description |
| --- | --- |
| `/msg <user> <message>` | Private message (aliases `/w`, `/pm`, `/whisper`, `/tell`) |
| `@<user> <message>` | Shortcut for a private message |
| `/r <message>` | Reply to the last person who sent you a private message |
| `/users` | List connected users (alias `/who`) |
| `/help` | Show the command list |
| `/quit` | Leave the chat |

Anything else is sent to everyone. Nicknames are case-insensitive when you address someone (`/msg BOB hi` reaches `bob`). Only the recipient gets a private message, and the sender gets a copy as confirmation (`[PM to bob] ...`). Unknown users, and sending a message to yourself, return an error.

**GUI:** the list on the right shows who is online. Select **Everyone** for public chat, or select a user so that what you type goes to them privately (double-click a user to jump to the input box). Private messages are shown in purple. All the text commands above also work in the GUI input box.

## Protocol

Newline-delimited JSON over TCP. The message types are documented in `chat/protocol.py`.

## Tests

```bash
python -m unittest -v
```
