"""
porygon_send.py — tiny local GUI to send a message (optionally with one
attached image) as the real Porygon bot account, via the Discord Bot API.

No separate webhook to manage: this reuses the same DISCORD_BOT_TOKEN the
production loop already uses, so there's only ever one credential to keep
up to date (see .env.example) instead of a webhook URL that can drift out
of sync with it.

Setup (one-time):
    Copy .env.example to .env in this folder and fill in the real values
    (same DISCORD_BOT_TOKEN/DISCORD_REACTION_CHANNEL_ID as the repo's GH
    secret/variable). .env is gitignored, never commit it.

Run:
    python porygon_send.py
"""
import os
import tkinter as tk
from tkinter import filedialog, messagebox

import discord_roles

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path: str):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file(os.path.join(_HERE, ".env"))

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.environ.get("DISCORD_REACTION_CHANNEL_ID")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Send as Porygon")
        self.geometry("420x340")
        self.attachment_path = None

        if not TOKEN or not CHANNEL_ID:
            messagebox.showerror(
                "Missing config",
                "DISCORD_BOT_TOKEN / DISCORD_REACTION_CHANNEL_ID not set.\n"
                "Copy .env.example to .env in this folder and fill them in.",
            )

        tk.Label(self, text="Message:").pack(anchor="w", padx=8, pady=(8, 0))
        self.text = tk.Text(self, height=10, wrap="word")
        self.text.pack(fill="both", expand=True, padx=8)

        row = tk.Frame(self)
        row.pack(fill="x", padx=8, pady=8)
        self.attach_label = tk.Label(row, text="No image attached", fg="gray")
        self.attach_label.pack(side="left")
        tk.Button(row, text="Clear", command=self._clear_attachment).pack(side="right", padx=(4, 0))
        tk.Button(row, text="Attach Image...", command=self._pick_file).pack(side="right")

        self.status = tk.Label(self, text="", anchor="w", fg="gray")
        self.status.pack(fill="x", padx=8)

        tk.Button(self, text="Send", command=self._send, bg="#5865F2", fg="white").pack(pady=8)

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Attach image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.webp"), ("All files", "*.*")],
        )
        if path:
            self.attachment_path = path
            self.attach_label.config(text=os.path.basename(path), fg="black")

    def _clear_attachment(self):
        self.attachment_path = None
        self.attach_label.config(text="No image attached", fg="gray")

    def _send(self):
        content = self.text.get("1.0", "end").strip()
        if not content and not self.attachment_path:
            self.status.config(text="Nothing to send.", fg="red")
            return
        if not TOKEN or not CHANNEL_ID:
            self.status.config(text="Missing config — see .env.example.", fg="red")
            return
        self.status.config(text="Sending...", fg="gray")
        self.update_idletasks()
        msg_id = discord_roles.post_message_with_file(CHANNEL_ID, TOKEN, content, self.attachment_path)
        if msg_id:
            self.status.config(text=f"Sent (message {msg_id}).", fg="green")
            self.text.delete("1.0", "end")
            self._clear_attachment()
        else:
            self.status.config(text="Failed to send — check console output.", fg="red")


if __name__ == "__main__":
    App().mainloop()
