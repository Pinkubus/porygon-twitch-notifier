"""
z_approve.py — father's quick approval popup for Z's summoned replies.

Launched by the watcher (porygon_z.ApprovalSession) as its own process the
moment father summons Z with an @ or a jugglez react. Talks over stdin/stdout,
one JSON object per line.

Phase 1 (ask): a small always-on-top window, "review Z's reply?", with a
countdown. Enter (or the Review button) → prints {"action": "review"} and
turns into the review window. Esc, closing it, or the countdown running out →
prints {"action": "skip"} and exits; Z posts as normal.

Phase 2 (review): shows "drafting…" until the watcher sends {"line": "..."},
then the proposed reply (editable). Buttons:
  Send (Enter)            → {"action": "send", "line": <text in the box>}
  Redo with feedback      → {"action": "feedback", "feedback": ..., "line": ...}
  Re-roll                 → {"action": "reroll", "line": ...}
  Don't send              → {"action": "cancel"}
Esc or closing the window sends the line as it stands (let it rip). While a
redo runs the watcher may send {"status": "..."} and later a new {"line"}.
"""
from __future__ import annotations

import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk

BG = "#0F1528"
PANEL = "#18213D"
PANEL_HI = "#22305A"
PINK = "#F2668B"
PINK_DEEP = "#C93D6E"
BLUE_DEEP = "#2A6FB5"
YELLOW = "#F8D34A"
TEXT = "#E9F1FF"
MUTED = "#8494BD"
DIM = "#56648C"
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI Semibold", 10)
FONT_H = ("Bahnschrift SemiBold", 13)
FONT_LINE = ("Segoe UI", 12)


def _debug(msg: str) -> None:
    """Why a popup did what it did, for when a review starts nobody asked for."""
    try:
        import os
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "z_approve.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


_vdm = None


def _desktop_manager():
    """Windows' documented IVirtualDesktopManager, or None."""
    global _vdm
    if _vdm is None:
        try:
            import comtypes
            import comtypes.client
            from ctypes import HRESULT, POINTER, c_int
            from ctypes.wintypes import HWND
            from comtypes import GUID, COMMETHOD, IUnknown

            class IVirtualDesktopManager(IUnknown):
                _iid_ = GUID("{a5cd92ff-29be-454c-8d04-d82879fb3f1b}")
                _methods_ = [
                    COMMETHOD([], HRESULT, "IsWindowOnCurrentVirtualDesktop",
                              (["in"], HWND, "hwnd"), (["out"], POINTER(c_int), "onCurrent")),
                    COMMETHOD([], HRESULT, "GetWindowDesktopId",
                              (["in"], HWND, "hwnd"), (["out"], POINTER(GUID), "desktopId")),
                    COMMETHOD([], HRESULT, "MoveWindowToDesktop",
                              (["in"], HWND, "hwnd"), (["in"], POINTER(GUID), "desktopId")),
                ]
            comtypes.CoInitialize()
            _vdm = comtypes.client.CreateObject(
                GUID("{aa509086-5ca9-4c25-8f95-589d3c07b48a}"), interface=IVirtualDesktopManager)
        except Exception:
            _vdm = False
    return _vdm or None


def follow_active_desktop(root: tk.Tk) -> bool:
    """Move this window onto whichever virtual desktop the user is looking
    at (the one holding the current foreground window). Porygon's console
    often lives on desktop 2 or 3, and a popup opened from there would
    otherwise appear over there, unseen. True if it moved."""
    vdm = _desktop_manager()
    if not vdm:
        return False
    try:
        from ctypes import byref
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        if vdm.IsWindowOnCurrentVirtualDesktop(hwnd):
            return False
        fg = user32.GetForegroundWindow()
        if not fg or fg == hwnd:
            return False
        desk = vdm.GetWindowDesktopId(fg)
        vdm.MoveWindowToDesktop(hwnd, byref(desk))
        _debug(f"moved to the active desktop (was elsewhere); now on current: "
               f"{bool(vdm.IsWindowOnCurrentVirtualDesktop(hwnd))}")
        return True
    except Exception as e:
        _debug(f"desktop follow failed: {e!r}")
        return False


def force_foreground(root: tk.Tk) -> None:
    """Windows won't let a background process take focus outright. Borrowing
    the foreground window's input queue for a moment is the polite way past
    that (no synthetic keystrokes), so Enter/Esc reach this window."""
    root.update_idletasks()
    follow_active_desktop(root)
    root.lift()
    root.attributes("-topmost", True)
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        me = kernel32.GetCurrentThreadId()
        title = ctypes.create_unicode_buffer(200)
        user32.GetWindowTextW(fg, title, 200)
        attached = ok = None
        if fg_thread and fg_thread != me:
            attached = user32.AttachThreadInput(me, fg_thread, True)
            ok = user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.AttachThreadInput(me, fg_thread, False)
        else:
            ok = user32.SetForegroundWindow(hwnd)
        root.focus_force()
        root.update()
        now_fg = user32.GetForegroundWindow()
        _debug(f"focus grab: had focus={title.value!r} attach={attached} set_fg={ok} "
               f"we_are_foreground={now_fg == hwnd} visible={bool(user32.IsWindowVisible(hwnd))}")
    except Exception as e:
        _debug(f"focus grab failed: {e!r}")
        root.focus_force()


def button(master, text, cmd, color=PANEL_HI):
    return tk.Button(master, text=text, command=cmd, font=FONT_B, fg=TEXT, bg=color,
                     activebackground=color, activeforeground=TEXT, relief="flat",
                     padx=12, pady=5, cursor="hand2", bd=0)


class Approver:
    def __init__(self, info: dict):
        self.info = info
        self.seconds = int(info.get("seconds", 5))
        self.phase = "ask"
        self.opened = time.monotonic()
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.root = tk.Tk()
        self.root.title("Porygon Z")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_escape)
        self.root.bind("<Escape>", lambda e: self.on_escape())
        self.root.bind("<Return>", self.on_enter)
        self.root.bind_all("<KeyPress>", lambda e: _debug(f"key {e.keysym} phase={self.phase}"), add="+")
        self.root.bind("<FocusIn>", lambda e: _debug("focus in") if e.widget is self.root else None, add="+")
        self.root.bind("<FocusOut>", self.on_focus_out, add="+")
        _debug(f"opened: {info.get('kind')} in #{info.get('channel')}")
        self.body = tk.Frame(self.root, bg=PANEL, padx=18, pady=14)
        self.body.pack(fill="both", expand=True, padx=2, pady=2)
        self.build_ask()
        self.place(420, 150)
        self.root.after(50, lambda: force_foreground(self.root))
        self.root.after(400, lambda: force_foreground(self.root))
        threading.Thread(target=self.read_stdin, daemon=True).start()
        self.root.after(100, self.poll_inbox)
        self.root.after(700, self.keep_on_active_desktop)
        self.tick()

    def on_focus_out(self, event):
        """During the 5-second ask, anything that takes focus away (Discord
        reclaiming it, a click elsewhere) gets it taken straight back, so
        Enter/Esc always land here."""
        if event.widget is not self.root:
            return
        _debug("focus out")
        if self.phase == "ask":
            self.root.after(60, lambda: force_foreground(self.root))

    def keep_on_active_desktop(self):
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            # Always follow; only grab focus during the 5-second ask, so a
            # review window left open doesn't steal keystrokes on every switch.
            if follow_active_desktop(self.root) and self.phase == "ask":
                force_foreground(self.root)
            self.root.after(700, self.keep_on_active_desktop)
        except tk.TclError:
            pass

    def place(self, w, h):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{sw - w - 30}+{sh - h - 80}")

    # --- phase 1 ---------------------------------------------------------------
    def build_ask(self):
        who = self.info.get("who") or "you"
        where = self.info.get("channel") or "?"
        tk.Label(self.body, text="Review Z's reply before it posts?", font=FONT_H,
                 fg=PINK, bg=PANEL, anchor="w").pack(fill="x")
        tk.Label(self.body, text=f"{self.info.get('kind', 'summon')} from {who} in #{where}",
                 font=FONT, fg=MUTED, bg=PANEL, anchor="w").pack(fill="x", pady=(2, 8))
        row = tk.Frame(self.body, bg=PANEL)
        row.pack(fill="x")
        button(row, "Review (Enter)", lambda: (_debug(f"review button clicked t={time.monotonic() - self.opened:.2f}s"),
                                               self.start_review()), PINK_DEEP).pack(side="left")
        button(row, "Let it rip (Esc)", self.on_escape).pack(side="left", padx=(8, 0))
        self.count = tk.Label(row, text="", font=FONT_B, fg=YELLOW, bg=PANEL)
        self.count.pack(side="right")

    def tick(self):
        if self.phase != "ask":
            return
        if self.seconds <= 0:
            _debug("countdown ran out")
            self.on_escape()
            return
        self.count.config(text=f"{self.seconds}s")
        self.seconds -= 1
        self.root.after(1000, self.tick)

    def start_review(self):
        if self.phase != "ask":
            return
        self.phase = "review"
        emit({"action": "review"})
        for w in self.body.winfo_children():
            w.destroy()
        self.build_review()
        self.place(620, 380)
        force_foreground(self.root)

    # --- phase 2 ---------------------------------------------------------------
    def build_review(self):
        tk.Label(self.body, text="Z's proposed reply", font=FONT_H, fg=PINK, bg=PANEL,
                 anchor="w").pack(fill="x")
        msg = self.info.get("message") or ""
        if msg:
            tk.Label(self.body, text=f"replying to {self.info.get('who', '')}: {msg[:220]}",
                     font=FONT, fg=MUTED, bg=PANEL, anchor="w", justify="left",
                     wraplength=570).pack(fill="x", pady=(2, 6))
        self.line = tk.Text(self.body, height=4, font=FONT_LINE, fg=TEXT, bg=PANEL_HI,
                            insertbackground=TEXT, relief="flat", wrap="word", padx=8, pady=6)
        self.line.pack(fill="x")
        self.line.bind("<Return>", self.on_enter)
        self.status = tk.Label(self.body, text="drafting… (a minute or so)", font=FONT,
                               fg=YELLOW, bg=PANEL, anchor="w")
        self.status.pack(fill="x", pady=(4, 6))
        tk.Label(self.body, text="Redo with feedback (blank = just re-roll)", font=FONT,
                 fg=MUTED, bg=PANEL, anchor="w").pack(fill="x")
        self.feedback = tk.Entry(self.body, font=FONT, fg=TEXT, bg=PANEL_HI,
                                 insertbackground=TEXT, relief="flat")
        self.feedback.pack(fill="x", ipady=4)
        self.feedback.bind("<Return>", lambda e: (self.redo(), "break")[1])
        row = tk.Frame(self.body, bg=PANEL)
        row.pack(fill="x", pady=(10, 0))
        self.btns = [
            button(row, "Send (Enter)", self.send, PINK_DEEP),
            button(row, "Redo with feedback", self.redo, BLUE_DEEP),
            button(row, "Re-roll", self.reroll),
            button(row, "Don't send", self.cancel),
        ]
        for i, b in enumerate(self.btns):
            b.pack(side="left", padx=(0 if i == 0 else 8, 0))
        self.set_busy(True)

    def set_busy(self, busy: bool):
        for b in self.btns:
            b.config(state="disabled" if busy else "normal")
        self.busy = busy

    def current(self) -> str:
        return self.line.get("1.0", "end").strip()

    def send(self):
        if self.phase == "review" and not self.busy and self.current():
            emit({"action": "send", "line": self.current()})
            self.root.destroy()

    def redo(self):
        if self.phase != "review" or self.busy:
            return
        fb = self.feedback.get().strip()
        if not fb:
            return self.reroll()
        emit({"action": "feedback", "feedback": fb, "line": self.current()})
        self.feedback.delete(0, "end")
        self.status.config(text="revising with your note…", fg=YELLOW)
        self.set_busy(True)

    def reroll(self):
        if self.phase != "review" or self.busy:
            return
        emit({"action": "reroll", "line": self.current()})
        self.status.config(text="re-rolling from scratch… (a minute or so)", fg=YELLOW)
        self.set_busy(True)

    def cancel(self):
        emit({"action": "cancel"})
        self.root.destroy()

    # --- keys --------------------------------------------------------------------
    def on_enter(self, event=None):
        if self.phase == "ask":
            _debug(f"enter key: widget={getattr(event, 'widget', None)} state={getattr(event, 'state', None)} "
                   f"t={time.monotonic() - self.opened:.2f}s")
            self.start_review()
        elif event is not None and event.widget is self.line and event.state & 0x0001:
            return None  # Shift+Enter: newline in the line box
        else:
            self.send()
        return "break"

    def on_escape(self):
        if self.phase == "ask":
            self.phase = "done"
            emit({"action": "skip"})
            self.root.destroy()
        elif self.phase == "review":
            if self.busy and not self.current():
                emit({"action": "skip"})  # nothing drafted yet: Z posts its own
            else:
                emit({"action": "send", "line": self.current()})
            self.root.destroy()

    # --- watcher messages --------------------------------------------------------------
    def read_stdin(self):
        for raw in sys.stdin:
            try:
                self.inbox.put(json.loads(raw))
            except ValueError:
                continue
        self.inbox.put({"closed": True})

    def poll_inbox(self):
        try:
            while True:
                msg = self.inbox.get_nowait()
                if msg.get("closed"):
                    self.root.destroy()
                    return
                if self.phase != "review":
                    continue
                if "line" in msg:
                    self.line.delete("1.0", "end")
                    self.line.insert("1.0", msg["line"])
                    self.status.config(text=msg.get("note") or "Enter sends it. Edit the text freely first.",
                                       fg=MUTED)
                    self.set_busy(False)
                    force_foreground(self.root)
                    self.line.focus_set()
                elif "status" in msg:
                    self.status.config(text=msg["status"], fg=YELLOW)
                    if msg.get("ready"):
                        self.set_busy(False)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.root.after(100, self.poll_inbox)


def main():
    info = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    Approver(info).root.mainloop()


if __name__ == "__main__":
    main()
