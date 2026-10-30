"""Tkinter GUI client with a user list for sending private messages.

Run:  python -m chat.gui_client [--host 127.0.0.1] [--port 5000]

Pick "Everyone" or a user in the list on the right to choose who receives
what you type. Double-click a user to start a private message. The text
commands (/msg, @user, /r, /users, /help) also work in the input box.
"""

import argparse
import queue
import tkinter as tk
from tkinter import messagebox, simpledialog

from . import protocol
from .client import ChatConnection

EVERYONE = "Everyone"


class ChatApp:
    def __init__(self, root: tk.Tk, host: str, port: int):
        self.root = root
        self.inbox: queue.Queue = queue.Queue()  # network thread -> Tk thread
        self.conn: ChatConnection | None = None
        self.joined = False

        root.title("Chat Room")
        root.geometry("720x480")
        root.minsize(480, 320)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        main = tk.Frame(root)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.log = tk.Text(main, state=tk.DISABLED, wrap=tk.WORD)
        self.log.tag_configure("private", foreground="#8a2be2")
        self.log.tag_configure("system", foreground="#777777", font=("TkDefaultFont", 9, "italic"))
        self.log.tag_configure("error", foreground="#c00000")
        scroll = tk.Scrollbar(main, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)

        side = tk.Frame(main)
        tk.Label(side, text="Send to:").pack(anchor=tk.W)
        self.users = tk.Listbox(side, width=18, exportselection=False)
        self.users.pack(fill=tk.Y, expand=True)
        self.users.bind("<<ListboxSelect>>", lambda _e: self._update_target_label())
        self.users.bind("<Double-Button-1>", lambda _e: self.entry.focus_set())

        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        bottom = tk.Frame(root)
        bottom.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.target_label = tk.Label(bottom, width=16, anchor=tk.W)
        self.target_label.pack(side=tk.LEFT)
        self.entry = tk.Entry(bottom)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry.bind("<Return>", lambda _e: self.submit())
        tk.Button(bottom, text="Send", command=self.submit).pack(side=tk.LEFT, padx=(8, 0))

        self._set_users([])
        try:
            self.conn = ChatConnection(host, port, self.inbox.put,
                                       lambda: self.inbox.put({"type": "_disconnected"}))
        except OSError as exc:
            messagebox.showerror("Chat Room", f"Could not connect to {host}:{port}\n{exc}")
            root.destroy()
            return
        self.ask_nick()
        self.root.after(50, self.poll)

    # -- helpers ---------------------------------------------------------
    def target(self) -> str:
        selection = self.users.curselection()
        return self.users.get(selection[0]) if selection else EVERYONE

    def _update_target_label(self) -> None:
        target = self.target()
        self.target_label.configure(text="To everyone:" if target == EVERYONE else f"PM {target}:")

    def _set_users(self, names: list[str]) -> None:
        current = self.target()
        me = self.conn.name if self.conn else None
        entries = [EVERYONE] + [n for n in names if n != me]
        self.users.delete(0, tk.END)
        for name in entries:
            self.users.insert(tk.END, name)
        index = entries.index(current) if current in entries else 0
        self.users.selection_set(index)
        self._update_target_label()

    def append(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n", (tag,) if tag else ())
        self.log.configure(state=tk.DISABLED)
        self.log.see(tk.END)

    def ask_nick(self) -> None:
        name = simpledialog.askstring("Chat Room", "Choose a nickname:", parent=self.root)
        if not name:
            self.quit()
            return
        self.conn.set_nick(name.strip())

    # -- event handling --------------------------------------------------
    def poll(self) -> None:
        try:
            while True:
                self.handle(self.inbox.get_nowait())
        except queue.Empty:
            pass
        if self.conn is not None:
            self.root.after(50, self.poll)

    def handle(self, message: dict) -> None:
        kind = message["type"]
        if kind == "_disconnected":
            self.append("*** Disconnected from server.", "error")
            self.entry.configure(state=tk.DISABLED)
            self.conn = None
            return
        if kind == "users":
            self._set_users(message["users"])
            return
        if kind == "welcome":
            self.joined = True
            self.root.title(f"Chat Room - {message['name']}")
        elif kind == "error" and not self.joined:
            messagebox.showwarning("Chat Room", message["text"])
            self.ask_nick()
            return
        tag = {"private": "private", "system": "system", "welcome": "system",
               "error": "error"}.get(kind)
        self.append(protocol.format_message(message, self.conn.name), tag)

    def submit(self) -> None:
        line = self.entry.get()
        if not line.strip() or self.conn is None:
            return
        self.entry.delete(0, tk.END)
        try:
            message = protocol.parse_input(line, self.conn.last_sender)
        except protocol.CommandError as exc:
            self.append(f"!!! {exc}", "error")
            return
        if message["type"] == "help":
            self.append(protocol.HELP_TEXT, "system")
            return
        if message["type"] == "quit":
            self.quit()
            return
        # Plain text goes to whoever is selected in the user list.
        if message["type"] == "public" and self.target() != EVERYONE:
            message = {"type": "private", "to": self.target(), "text": message["text"]}
        if message["type"] == "users":
            self.append("Online: " + ", ".join(self.users.get(1, tk.END) + (self.conn.name,)), "system")
            return
        try:
            self.conn.send(message)
        except OSError:
            self.append("!!! Failed to send message.", "error")

    def quit(self) -> None:
        if self.conn is not None:
            conn, self.conn = self.conn, None
            conn.on_disconnect = None
            conn.close()
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chat room GUI client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)
    root = tk.Tk()
    ChatApp(root, args.host, args.port)
    root.mainloop()


if __name__ == "__main__":
    main()
