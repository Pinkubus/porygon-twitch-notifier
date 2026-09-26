"""
porygon_panel.py — desktop control panel for Porygon Z.

Shows, live:
  * every human member of the server, with when Z last replied to them —
    the row flips to an animated "preparing reply..." while Z is drafting
    one for that person, then to the new reply time once it posts. A "stop"
    button appears on that row while it's busy: it tells the watcher to
    drop this specific draft right before it would have posted (see
    z_live.request_cancel/consume_cancel), so father can answer with extra
    context himself instead of the auto-draft landing first or racing it;
  * what Z is up to right now (drafting / idle / paused / offline);
  * the activity.log feed;
  * every `!feature` suggestion the server has posted, collapsed to a
    five-word title you can click open (see feature_requests.py).

And lets you steer Z while it runs: pause it, switch individual behaviors
on/off, and turn its humor bars, draft counts, cooldowns, and odds. Changes
go to z_controls.json, which the watcher re-reads every scan (~10s) — no
restart needed. Anything you haven't touched falls back to .env.

It only reads/writes local files (see z_live.py) and never talks to the
watcher directly, so it's safe to open and close at any time.

Run:  python porygon_panel.py      (or start_panel.bat for no console)
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)


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


# Load .env before importing porygon_z, so its captured defaults match what
# the watcher sees.
_load_env_file(os.path.join(_HERE, ".env"))

import activity_log  # noqa: E402
import discord_roles  # noqa: E402
import feature_requests  # noqa: E402
import porygon_names  # noqa: E402
import porygon_voice  # noqa: E402
import porygon_z  # noqa: E402
import z_brain  # noqa: E402
import z_live  # noqa: E402

LOG_FILE = os.path.join(_HERE, "activity.log")
FEATURES_FILE = feature_requests.REQUESTS_FILE
MEMBERS_CACHE = os.path.join(_HERE, "z_members_cache.json")
WATCHER_SCRIPT = "quotes_watch_local.py"
ICON_PATH = r"C:\Users\Williwaugh\Desktop\emotes\porygoncuter.png"

HEARTBEAT_FRESH_SECONDS = 90
# Raw Discord user ids in activity.log lines, swapped for display names.
_ID_RE = re.compile(r"\b\d{17,20}\b")
STALE_PROCESSING_SECONDS = 20 * 60

# --- Porygon palette ---------------------------------------------------------
# Porygon's pink + sky-blue polygons, Porygon-Z's deep navy body, yellow eyes
# and red eye-rings.
BG = "#0F1528"
PANEL = "#18213D"
PANEL_HI = "#22305A"
LINE = "#2C3B66"
PINK = "#F2668B"
PINK_DEEP = "#C93D6E"
BLUE = "#4DB6EC"
BLUE_DEEP = "#2A6FB5"
YELLOW = "#F8D34A"
RED = "#EF4B4F"
VIOLET = "#B08CFF"
TEXT = "#E9F1FF"
MUTED = "#8494BD"
DIM = "#56648C"

FONT_TITLE = ("Bahnschrift SemiBold", 20)
FONT_H = ("Bahnschrift SemiBold", 12)
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI Semibold", 10)
FONT_SM = ("Segoe UI", 8)
FONT_MONO = ("Consolas", 9)

# --- control definitions -----------------------------------------------------
SWITCHES = [
    # key, label, description, "on" means bad (drawn red)
    ("paused", "Pause Z", "Keeps reading, answers nothing. No backlog on resume.", True),
    ("unprompted", "Unprompted replies", "Z chiming in on its own when a line clears the bar.", False),
    ("six", "\u201cSix when I get there\u201d", "The glitchy number-guess callback.", False),
    ("quote_callback", "Quote-callback replies", "A real reply on top of a porygonwow reaction.", False),
    ("polls", "!poll commands", "Reads back the channel and files Discord polls on request.", False),
]


def _fmt_score(v):
    return f"{v:.1f} / 7"


def _fmt_int(v):
    return f"{int(v)}"


def _fmt_minutes(v):
    m = int(round(v / 60))
    if m == 0:
        return "off"
    return f"{m} min" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def _fmt_pct(v):
    return f"{v * 100:.0f}%"


SLIDERS = [
    # key, label, lo, hi, step, formatter, color
    ("auto_score_threshold", "Unprompted humor bar", 1.0, 7.0, 0.1, _fmt_score, PINK),
    ("command_min_score", "Summon humor bar", 1.0, 7.0, 0.1, _fmt_score, PINK),
    ("command_drafts", "Drafts per summon / !z", 1, 25, 1, _fmt_int, BLUE),
    ("auto_drafts", "Drafts per unprompted try", 1, 25, 1, _fmt_int, BLUE),
    ("auto_cooldown_seconds", "Unprompted cooldown (per channel)", 0, 4 * 3600, 60, _fmt_minutes, YELLOW),
    ("six_cooldown_seconds", "\u201cSix\u201d cooldown (per channel)", 0, 2 * 3600, 60, _fmt_minutes, YELLOW),
]

KNOBS = [
    ("react_chance", "React instead", 0.0, 1.0, 0.01, PINK),
    ("callback_reply_chance", "Callback odds", 0.0, 1.0, 0.01, BLUE),
    ("glitch_rate", "Glitch", 0.0, 1.0, 0.01, VIOLET),
    ("tone_shift_rate", "Tone shift", 0.0, 1.0, 0.01, YELLOW),
]

KIND_LABEL = {
    "!z": "!z",
    "summon": "summon",
    "unprompted": "unprompted",
    "quote callback": "quote-callback",
    "six": "six",
    "jugglez": "jugglez-react",
    "!poll": "!poll",
    "!poll answer": "!poll",
}


# --- helpers ------------------------------------------------------------------

def _rel_time(ts: float) -> str:
    delta = max(0, time.time() - ts)
    if delta < 45:
        rel = "just now"
    elif delta < 3600:
        rel = f"{int(delta // 60) or 1}m ago"
    elif delta < 86400:
        rel = f"{int(delta // 3600)}h ago"
    else:
        rel = f"{int(delta // 86400)}d ago"
    dt = datetime.datetime.fromtimestamp(ts)
    clock = dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() != datetime.date.today():
        clock = dt.strftime("%b %d ") + clock
    return f"{rel} \u00b7 {clock}"


def _parse_utc(stamp: str) -> float:
    """feature_requests.json's "%Y-%m-%dT%H:%M:%SZ" stamp -> epoch seconds."""
    try:
        return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc,
        ).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _dots(period: float = 0.45) -> str:
    n = int(time.time() / period) % 3 + 1
    return ("." * n).ljust(3)


def _watcher_pids() -> list[int]:
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
                "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }",
            ],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    pids = []
    for line in result.stdout.splitlines():
        pid, _, cmd = line.partition("|")
        if pid.strip().isdigit() and WATCHER_SCRIPT in cmd:
            pids.append(int(pid))
    return pids


def _fetch_members() -> dict[str, str]:
    """uid -> display name for every non-bot member. Needs the Server
    Members Intent enabled on the main bot in the Discord Developer Portal."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild_id:
        raise RuntimeError("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID missing from .env")
    members: dict[str, str] = {}
    after = "0"
    while True:
        resp = requests.get(
            f"{discord_roles.DISCORD_API}/guilds/{guild_id}/members",
            headers=discord_roles._headers(token),
            params={"limit": 1000, "after": after},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        batch = resp.json()
        for m in batch:
            user = m.get("user", {})
            if user.get("bot"):
                continue
            members[user["id"]] = m.get("nick") or user.get("global_name") or user.get("username")
        if len(batch) < 1000:
            return members
        after = batch[-1]["user"]["id"]


def _fetch_text_channels() -> list[tuple[str, str]]:
    """(channel_id, "#name") for every text channel in every configured
    guild, freshest call wins — this is the "live-updated list" the manual
    tool's dropdown is built from, refetched on demand rather than cached,
    since a channel created/renamed minutes ago should show up immediately."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN missing from .env")
    out: list[tuple[str, str]] = []
    for guild_id in discord_roles.configured_guild_ids():
        for channel in discord_roles.get_guild_text_channels(guild_id, token):
            out.append((channel["id"], f"#{channel.get('name', channel['id'])}"))
    return out


# A prompt that's nothing but a shortcode — `:porygonspin:` — means "react
# with this", not "compose a message". \w keeps it to Discord's own emoji-name
# alphabet (letters, digits, underscore), 2-32 chars like the real limit.
_EMOTE_SHORTCODE_RE = re.compile(r"^:(\w{2,32}):$")


def _resolve_custom_emoji(name: str) -> Optional[str]:
    """`name` (no colons) -> `name:id` reaction form, searching every
    configured guild's custom emoji, case-insensitively. None if nothing
    matches — there's no attempt at standard Unicode shortcodes (:fire:
    and the like), only this server's own custom set, since that's what a
    shortcode typed into this panel is almost always reaching for."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        return None
    lowered = name.lower()
    for guild_id in discord_roles.configured_guild_ids():
        for emoji in discord_roles.get_guild_emojis(guild_id, token):
            if (emoji.get("name") or "").lower() == lowered:
                return f"{emoji['name']}:{emoji['id']}"
    return None


def _git_commit_push(paths: list[str], message: str) -> None:
    """Best-effort commit+push for the handful of files a manual send can
    touch (reply history, name-coinage files, the activity log).

    The panel isn't the watcher: it has none of quotes_watch_local.py's
    stuck-rebase/stuck-editor recovery, because a manual send is rare and
    human-paced, not a 10-second loop — a failed push here just leaves the
    files modified on disk for the next real commit (the watcher's own next
    cycle, or a person) to pick up. Never raises; a git hiccup must not make
    the send itself look like it failed when the post already went up.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        existing = [p for p in paths if os.path.exists(os.path.join(_HERE, p))]
        if not existing:
            return
        subprocess.run(["git", "-C", _HERE, "add", *existing], env=env, timeout=15)
        staged = subprocess.run(
            ["git", "-C", _HERE, "diff", "--cached", "--quiet", "--", *existing],
            env=env, timeout=15,
        )
        if staged.returncode == 0:
            return  # nothing actually changed
        subprocess.run(["git", "-C", _HERE, "commit", "-m", message], env=env, timeout=15)
        subprocess.run(["git", "-C", _HERE, "push"], env=env, timeout=30)
    except Exception as e:
        print(f"[panel] manual-send commit/push failed (non-fatal): {e}")


def _known_members() -> dict[str, str]:
    """Fallback roster when the member list can't be fetched: everyone Z has
    profiled, plus anyone it has replied to."""
    members = {}
    try:
        with open(MEMBERS_CACHE, encoding="utf-8") as f:
            members.update(json.load(f))
    except Exception:
        pass
    for uid, p in z_brain.load_profiles().items():
        if not uid.startswith("_") and isinstance(p, dict) and p.get("name"):
            members.setdefault(uid, p["name"])
    return members


# --- widgets -------------------------------------------------------------------

class Switch(tk.Canvas):
    W, H = 46, 24

    def __init__(self, master, command, danger=False):
        super().__init__(master, width=self.W, height=self.H, bg=PANEL, highlightthickness=0, cursor="hand2")
        self.on = False
        self.command = command
        self.danger = danger
        self.bind("<Button-1>", lambda e: self._toggle())
        self._draw()

    def set(self, on: bool):
        if on != self.on:
            self.on = on
            self._draw()

    def _toggle(self):
        self.on = not self.on
        self._draw()
        self.command(self.on)

    def _draw(self):
        self.delete("all")
        w, h, r = self.W, self.H, self.H / 2
        fill = (RED if self.danger else BLUE) if self.on else LINE
        self.create_oval(0, 0, h, h, fill=fill, outline=fill)
        self.create_oval(w - h, 0, w, h, fill=fill, outline=fill)
        self.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill)
        x = w - r if self.on else r
        self.create_oval(x - r + 3, 3, x + r - 3, h - 3, fill=TEXT, outline="")


class Slider(tk.Canvas):
    PAD = 10

    def __init__(self, master, lo, hi, step, color, command):
        super().__init__(master, height=24, bg=PANEL, highlightthickness=0, cursor="hand2")
        self.lo, self.hi, self.step, self.color, self.command = lo, hi, step, color, command
        self.value = lo
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<MouseWheel>", lambda e: self._change(self.value + (self.step if e.delta > 0 else -self.step)))
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, v):
        v = self._snap(v)
        if v != self.value:
            self.value = v
            self._draw()

    def _snap(self, v):
        v = min(self.hi, max(self.lo, v))
        return round(round((v - self.lo) / self.step) * self.step + self.lo, 6)

    def _draw(self):
        self.delete("all")
        w, y, p = self.winfo_width(), 12, self.PAD
        x = p + (w - 2 * p) * (self.value - self.lo) / (self.hi - self.lo)
        self.create_line(p, y, w - p, y, fill=LINE, width=6, capstyle="round")
        self.create_line(p, y, x, y, fill=self.color, width=6, capstyle="round")
        self.create_oval(x - 8, y - 8, x + 8, y + 8, fill=TEXT, outline=self.color, width=3)

    def _drag(self, e):
        w, p = self.winfo_width(), self.PAD
        self._change(self.lo + (e.x - p) / max(1, w - 2 * p) * (self.hi - self.lo))

    def _change(self, v):
        v = self._snap(v)
        if v != self.value:
            self.value = v
            self._draw()
            self.command(v)


class Knob(tk.Canvas):
    S = 68

    def __init__(self, master, lo, hi, step, color, command):
        super().__init__(master, width=self.S, height=self.S, bg=PANEL, highlightthickness=0,
                         cursor="sb_v_double_arrow")
        self.lo, self.hi, self.step, self.color, self.command = lo, hi, step, color, command
        self.value = lo
        self._drag_from = None
        self.bind("<Button-1>", lambda e: setattr(self, "_drag_from", (e.y, self.value)))
        self.bind("<B1-Motion>", self._drag)
        self.bind("<MouseWheel>", lambda e: self._change(self.value + (self.step if e.delta > 0 else -self.step) * 5))
        self._draw()

    def set(self, v):
        v = self._snap(v)
        if v != self.value:
            self.value = v
            self._draw()

    def _snap(self, v):
        v = min(self.hi, max(self.lo, v))
        return round(round((v - self.lo) / self.step) * self.step + self.lo, 6)

    def _draw(self):
        self.delete("all")
        c, r = self.S / 2, self.S / 2 - 7
        frac = (self.value - self.lo) / (self.hi - self.lo)
        box = (c - r, c - r, c + r, c + r)
        self.create_arc(*box, start=-45, extent=270, style="arc", outline=LINE, width=7)
        if frac > 0.001:
            self.create_arc(*box, start=225, extent=-270 * frac, style="arc", outline=self.color, width=7)
        body = r - 10
        self.create_oval(c - body, c - body, c + body, c + body, fill=PANEL_HI, outline=LINE, width=2)
        a = math.radians(225 - 270 * frac)
        self.create_line(
            c + 6 * math.cos(a), c - 6 * math.sin(a), c + (body - 4) * math.cos(a), c - (body - 4) * math.sin(a),
            fill=YELLOW, width=3, capstyle="round",
        )
        self.create_oval(c - 3, c - 3, c + 3, c + 3, fill=RED, outline="")

    def _drag(self, e):
        if self._drag_from is None:
            return
        y0, v0 = self._drag_from
        self._change(v0 + (y0 - e.y) / 150 * (self.hi - self.lo))

    def _change(self, v):
        v = self._snap(v)
        if v != self.value:
            self.value = v
            self._draw()
            self.command(v)


def _card(master, title: str) -> tuple[tk.Frame, tk.Frame, tk.Label]:
    """A titled panel; returns (outer, body, right-side header label)."""
    outer = tk.Frame(master, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
    head = tk.Frame(outer, bg=PANEL)
    head.pack(fill="x", padx=14, pady=(12, 6))
    tk.Frame(head, bg=PINK, width=4, height=16).pack(side="left", padx=(0, 8))
    tk.Label(head, text=title, font=FONT_H, fg=TEXT, bg=PANEL).pack(side="left")
    right = tk.Label(head, text="", font=FONT_SM, fg=MUTED, bg=PANEL)
    right.pack(side="right")
    body = tk.Frame(outer, bg=PANEL)
    body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
    return outer, body, right


def _button(master, text, command, color=BLUE_DEEP):
    return tk.Button(
        master, text=text, command=command, font=FONT_B, fg=TEXT, bg=color,
        activebackground=PINK_DEEP, activeforeground=TEXT, relief="flat", bd=0,
        padx=12, pady=5, cursor="hand2",
    )


# --- app -----------------------------------------------------------------------

class Panel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Porygon-Z Control Panel")
        self.configure(bg=BG)
        self.geometry(f"1320x{min(880, self.winfo_screenheight() - 80)}+20+10")
        self.minsize(1100, 720)
        try:
            self._icon = tk.PhotoImage(file=ICON_PATH)
            self.iconphoto(True, self._icon)
        except Exception:
            pass

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Z.Vertical.TScrollbar", background=PANEL_HI, troughcolor=PANEL, bordercolor=PANEL,
            arrowcolor=MUTED, lightcolor=PANEL_HI, darkcolor=PANEL_HI, gripcount=0,
        )

        self.status: dict = {}
        self.controls: dict = z_live.load_controls()
        self.defaults = dict(porygon_z._TUNABLE_DEFAULTS)
        self.members: dict[str, str] = _known_members()
        self.member_rows: dict[str, dict] = {}
        self.pids: list[int] = []
        self._save_job = None
        self._log_mtime = 0.0
        self._features_mtime = -1.0
        self._features_open: set[str] = set()
        self._z_only = tk.BooleanVar(value=False)

        self._build_header()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        body.columnconfigure(0, weight=0, minsize=330)
        body.columnconfigure(1, weight=0, minsize=430)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)
        body.rowconfigure(1, weight=0)
        body.rowconfigure(2, weight=0)
        self._build_members(body).grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._build_controls(body).grid(row=0, column=1, sticky="nsew", padx=(0, 12))
        self._build_feed(body).grid(row=0, column=2, sticky="nsew")
        # Full width: an expanded request shows the whole original message,
        # which needs the room to wrap.
        self._build_features(body).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self._build_manual(body).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))

        self._rebuild_member_rows()
        self._load_controls_into_widgets()
        self.refresh_members()
        self.refresh_manual_channels()
        self._poll_status()
        self._poll_log()
        self._poll_features()
        self._poll_procs()
        self._animate()

    # ---- header ----
    def _build_header(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 12))

        logo = tk.Canvas(head, width=74, height=54, bg=BG, highlightthickness=0)
        logo.pack(side="left", padx=(0, 12))
        # Low-poly Porygon-ish mark: pink head, blue beak, yellow eye / red ring.
        logo.create_polygon(6, 30, 26, 6, 52, 10, 58, 34, 34, 50, 12, 46, fill=PINK, outline="")
        logo.create_polygon(26, 6, 52, 10, 40, 22, fill=PINK_DEEP, outline="")
        logo.create_polygon(52, 22, 72, 30, 54, 38, fill=BLUE, outline="")
        logo.create_polygon(6, 30, 12, 46, 0, 42, fill=BLUE, outline="")
        logo.create_oval(28, 16, 46, 34, fill=RED, outline="")
        logo.create_oval(32, 20, 42, 30, fill=YELLOW, outline="")
        logo.create_oval(35, 23, 39, 27, fill=BG, outline="")

        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left")
        row = tk.Frame(titles, bg=BG)
        row.pack(anchor="w")
        tk.Label(row, text="PORYGON-Z", font=FONT_TITLE, fg=PINK, bg=BG).pack(side="left")
        tk.Label(row, text=" CONTROL PANEL", font=FONT_TITLE, fg=BLUE, bg=BG).pack(side="left")
        self.doing_label = tk.Label(titles, text="", font=FONT, fg=MUTED, bg=BG, anchor="w")
        self.doing_label.pack(anchor="w")

        right = tk.Frame(head, bg=BG)
        right.pack(side="right")
        self.pill = tk.Label(right, text="", font=("Bahnschrift SemiBold", 11), fg=BG, bg=MUTED, padx=12, pady=4)
        self.pill.pack(side="left", padx=(0, 14))
        self.stats_label = tk.Label(right, text="", font=FONT, fg=TEXT, bg=BG, justify="right")
        self.stats_label.pack(side="left", padx=(0, 14))
        _button(right, "\u25b6  Start watcher", self.start_watcher).pack(side="left", padx=(0, 6))
        _button(right, "\u25a0  Stop", self.stop_watcher, color=PINK_DEEP).pack(side="left")

        tk.Frame(self, bg=PINK, height=2).pack(fill="x", padx=16)
        tk.Frame(self, bg=BLUE, height=2).pack(fill="x", padx=16, pady=(0, 12))

    # ---- members ----
    def _build_members(self, master):
        outer, body, self.members_note = _card(master, "MEMBERS \u00b7 LAST REPLY")
        wrap = tk.Frame(body, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        self.members_canvas = tk.Canvas(wrap, bg=PANEL, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.members_canvas.yview, style="Z.Vertical.TScrollbar")
        self.members_inner = tk.Frame(self.members_canvas, bg=PANEL)
        self.members_inner.bind(
            "<Configure>", lambda e: self.members_canvas.configure(scrollregion=self.members_canvas.bbox("all")),
        )
        win = self.members_canvas.create_window((0, 0), window=self.members_inner, anchor="nw")
        self.members_canvas.bind("<Configure>", lambda e: self.members_canvas.itemconfigure(win, width=e.width))
        self.members_canvas.configure(yscrollcommand=sb.set)
        self.members_canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def _wheel(e):
            self.members_canvas.yview_scroll(int(-e.delta / 120), "units")
        self.members_canvas.bind("<Enter>", lambda e: self.members_canvas.bind_all("<MouseWheel>", _wheel))
        self.members_canvas.bind("<Leave>", lambda e: self.members_canvas.unbind_all("<MouseWheel>"))

        foot = tk.Frame(body, bg=PANEL)
        foot.pack(fill="x", pady=(8, 0))
        _button(foot, "\u21bb  Refresh members", self.refresh_members, color=PANEL_HI).pack(side="left")
        return outer

    def refresh_members(self):
        self.members_note.config(text="loading\u2026", fg=MUTED)

        def work():
            try:
                fetched = _fetch_members()
                try:
                    with open(MEMBERS_CACHE, "w", encoding="utf-8") as f:
                        json.dump(fetched, f, indent=2)
                except OSError:
                    pass
                self.after(0, lambda: self._members_loaded(fetched, f"{len(fetched)} members", MUTED))
            except Exception as e:
                note = (
                    "known members only \u2014 turn on Server Members Intent"
                    if "403" in str(e) else f"known members only ({e})"
                )
                self.after(0, lambda: self._members_loaded(_known_members(), note, YELLOW))

        threading.Thread(target=work, daemon=True).start()

    def _members_loaded(self, members: dict, note: str, color: str):
        self.members = members
        self.members_note.config(text=note, fg=color)
        self._rebuild_member_rows()
        self._render_features()  # roster names beat the author name stored per request

    def _roster(self) -> dict[str, str]:
        roster = dict(self.members)
        for uid, info in (self.status.get("last_reply") or {}).items():
            roster.setdefault(uid, info.get("name") or uid)
        proc = self.status.get("processing") or {}
        if proc.get("user_id"):
            roster.setdefault(proc["user_id"], proc.get("name") or proc["user_id"])
        return roster

    def _rebuild_member_rows(self):
        roster = self._roster()
        if set(roster) == set(self.member_rows) and all(
            self.member_rows[u]["name"].cget("text") == n for u, n in roster.items()
        ):
            return
        for child in self.members_inner.winfo_children():
            child.destroy()
        self.member_rows = {}
        for uid, name in sorted(roster.items(), key=lambda kv: kv[1].casefold()):
            row = tk.Frame(self.members_inner, bg=PANEL)
            row.pack(fill="x", pady=1)
            stripe = tk.Frame(row, bg=PANEL, width=4)
            stripe.pack(side="left", fill="y")
            inner = tk.Frame(row, bg=PANEL)
            inner.pack(side="left", fill="x", expand=True, padx=(8, 6), pady=4)
            top = tk.Frame(inner, bg=PANEL)
            top.pack(fill="x")
            name_lbl = tk.Label(top, text=name, font=FONT_B, fg=TEXT, bg=PANEL, anchor="w")
            name_lbl.pack(side="left")
            when_lbl = tk.Label(top, text="", font=FONT, fg=MUTED, bg=PANEL, anchor="e")
            when_lbl.pack(side="right")
            # Only ever shown (packed) while this row is the busy one — see
            # _update_member_rows. Lets father stop a reply it's drafting and
            # answer with extra context himself instead of waiting for it (or
            # racing it) to post.
            cancel_btn = tk.Button(
                top, text="stop", font=FONT_SM, fg=TEXT, bg=RED,
                activebackground=PINK_DEEP, activeforeground=TEXT, relief="flat", bd=0,
                padx=6, pady=1, cursor="hand2", command=lambda u=uid: self._cancel_processing(u),
            )
            sub_lbl = tk.Label(inner, text="", font=FONT_SM, fg=DIM, bg=PANEL, anchor="w", justify="left")
            sub_lbl.pack(fill="x")
            self.member_rows[uid] = {
                "row": row, "stripe": stripe, "frames": [row, inner, top],
                "name": name_lbl, "when": when_lbl, "sub": sub_lbl, "cancel": cancel_btn, "state": None,
            }
        self._update_member_rows()

    def _processing(self) -> dict | None:
        proc = self.status.get("processing")
        if not proc or time.time() - proc.get("since", 0) > STALE_PROCESSING_SECONDS:
            return None
        return proc

    def _update_member_rows(self):
        proc = self._processing()
        last = self.status.get("last_reply") or {}
        for uid, r in self.member_rows.items():
            if proc and proc.get("user_id") == uid:
                state = "busy"
                when = f"preparing reply{_dots()}"
                sub = f"{KIND_LABEL.get(proc.get('kind'), proc.get('kind'))} in #{proc.get('channel', '?')}"
                scores = proc.get("scores") or []
                if scores:
                    sub += f"  \u00b7  best so far {max(scores):g}"
            elif uid in last:
                info = last[uid]
                recent = time.time() - info.get("ts", 0) < 3600
                state = "recent" if recent else "old"
                when = _rel_time(info["ts"])
                text = (info.get("text") or "").replace("\n", " ")
                sub = f"{KIND_LABEL.get(info.get('kind'), info.get('kind'))}: {text[:48]}{'\u2026' if len(text) > 48 else ''}"
            else:
                state, when, sub = "never", "\u2014", ""

            if r["when"].cget("text") != when:
                r["when"].config(text=when)
            if r["sub"].cget("text") != sub:
                r["sub"].config(text=sub)
            if state != r["state"]:
                r["state"] = state
                bg = PANEL_HI if state == "busy" else PANEL
                for w in r["frames"] + [r["name"], r["when"], r["sub"]]:
                    w.config(bg=bg)
                r["stripe"].config(bg={"busy": PINK, "recent": BLUE}.get(state, bg))
                if state == "busy":
                    r["cancel"].config(text="stop", state="normal")
                    r["cancel"].pack(side="right", padx=(0, 6))
                else:
                    r["cancel"].pack_forget()
                r["when"].config(
                    fg={"busy": PINK, "recent": BLUE, "old": TEXT}.get(state, DIM),
                    font=("Consolas", 10, "bold") if state == "busy" else FONT,
                )
                r["sub"].config(fg=MUTED if state == "busy" else DIM)

    def _cancel_processing(self, user_id: str):
        """Stop button on a busy member row: the watcher checks for this
        right before it would have posted, so the draft already in flight
        (API calls already spent) still gets dropped rather than posted —
        it just doesn't land, leaving the field clear to answer by hand
        instead. Local file write, fast enough to run right on this thread."""
        row = self.member_rows.get(user_id)
        if row:
            row["cancel"].config(text="stopping…", state="disabled")
        if not z_live.request_cancel(user_id):
            # Finished (or never started) before the click landed — nothing
            # to stop. The row will already be flipping away from "busy" on
            # its own, so just leave it; no need to explain the miss.
            pass

    # ---- controls ----
    def _build_controls(self, master):
        outer, body, self.apply_label = _card(master, "CONTROLS")

        # Switches/sliders/knobs now outgrow the window (the manual-message
        # card pushed everything shorter), so this is a scrolling canvas —
        # same shape as _build_members/_build_features — with the footer
        # pinned below it rather than scrolling away with the rest.
        wrap = tk.Frame(body, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        self.controls_canvas = tk.Canvas(wrap, bg=PANEL, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.controls_canvas.yview, style="Z.Vertical.TScrollbar")
        inner = tk.Frame(self.controls_canvas, bg=PANEL)
        inner.bind(
            "<Configure>", lambda e: self.controls_canvas.configure(scrollregion=self.controls_canvas.bbox("all")),
        )
        win = self.controls_canvas.create_window((0, 0), window=inner, anchor="nw")
        self.controls_canvas.bind("<Configure>", lambda e: self.controls_canvas.itemconfigure(win, width=e.width))
        self.controls_canvas.configure(yscrollcommand=sb.set)
        self.controls_canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.switch_widgets = {}
        for key, label, desc, danger in SWITCHES:
            row = tk.Frame(inner, bg=PANEL)
            row.pack(fill="x", pady=(0, 6))
            sw = Switch(row, lambda on, k=key: self._set_control(k, on), danger=danger)
            sw.pack(side="right", padx=(8, 0))
            txt = tk.Frame(row, bg=PANEL)
            txt.pack(side="left", fill="x", expand=True)
            tk.Label(txt, text=label, font=FONT_B, fg=TEXT, bg=PANEL, anchor="w").pack(fill="x")
            tk.Label(txt, text=desc, font=FONT_SM, fg=MUTED, bg=PANEL, anchor="w").pack(fill="x")
            self.switch_widgets[key] = sw

        tk.Frame(inner, bg=LINE, height=1).pack(fill="x", pady=(2, 8))

        self.slider_widgets = {}
        for key, label, lo, hi, step, fmt, color in SLIDERS:
            box = tk.Frame(inner, bg=PANEL)
            box.pack(fill="x", pady=(0, 2))
            top = tk.Frame(box, bg=PANEL)
            top.pack(fill="x")
            name = tk.Label(top, text=label, font=FONT, fg=TEXT, bg=PANEL, anchor="w", cursor="hand2")
            name.pack(side="left")
            name.bind("<Double-Button-1>", lambda e, k=key: self._reset_one(k))
            val = tk.Label(top, text="", font=FONT_B, fg=color, bg=PANEL)
            val.pack(side="right")
            sl = Slider(box, lo, hi, step, color, lambda v, k=key: self._set_control(k, v))
            sl.pack(fill="x")
            self.slider_widgets[key] = (sl, val, fmt)

        tk.Frame(inner, bg=LINE, height=1).pack(fill="x", pady=(4, 6))

        knobs = tk.Frame(inner, bg=PANEL)
        knobs.pack(fill="x")
        self.knob_widgets = {}
        for i, (key, label, lo, hi, step, color) in enumerate(KNOBS):
            knobs.columnconfigure(i, weight=1)
            cell = tk.Frame(knobs, bg=PANEL)
            cell.grid(row=0, column=i)
            name = tk.Label(cell, text=label, font=FONT_SM, fg=MUTED, bg=PANEL, cursor="hand2")
            name.pack()
            name.bind("<Double-Button-1>", lambda e, k=key: self._reset_one(k))
            kn = Knob(cell, lo, hi, step, color, lambda v, k=key: self._set_control(k, v))
            kn.pack()
            val = tk.Label(cell, text="", font=FONT_B, fg=color, bg=PANEL)
            val.pack()
            self.knob_widgets[key] = (kn, val)

        # Sliders and knobs already use the wheel themselves (to nudge their
        # own value), so the page-scroll handler has to step aside for them
        # specifically rather than claiming every wheel event under the
        # canvas the way members'/features' plain-label lists safely can.
        def _wheel(e):
            if isinstance(e.widget, (Slider, Knob)):
                return
            self.controls_canvas.yview_scroll(int(-e.delta / 120), "units")
        self.controls_canvas.bind("<Enter>", lambda e: self.controls_canvas.bind_all("<MouseWheel>", _wheel))
        self.controls_canvas.bind("<Leave>", lambda e: self.controls_canvas.unbind_all("<MouseWheel>"))

        foot = tk.Frame(body, bg=PANEL)
        foot.pack(fill="x", side="bottom", pady=(10, 0))
        tk.Label(
            foot, text="Double-click a label to reset it. Drag knobs up/down or scroll.",
            font=FONT_SM, fg=DIM, bg=PANEL,
        ).pack(side="left")
        _button(foot, "Reset all to .env", self._reset_all, color=PANEL_HI).pack(side="right")
        return outer

    def _value(self, key):
        if key in self.controls:
            return self.controls[key]
        if key in self.defaults:
            return self.defaults[key]
        return porygon_z._SWITCH_DEFAULTS.get(key)

    def _load_controls_into_widgets(self):
        for key, sw in self.switch_widgets.items():
            sw.set(bool(self._value(key)))
        for key, (sl, val, fmt) in self.slider_widgets.items():
            v = self._value(key)
            sl.set(v)
            self._paint_value(key, val, fmt(v))
        for key, (kn, val) in self.knob_widgets.items():
            v = self._value(key)
            kn.set(v)
            self._paint_value(key, val, _fmt_pct(v))

    def _paint_value(self, key, label, text):
        overridden = key in self.controls and self.controls[key] != self.defaults.get(key)
        label.config(text=text + (" \u2022" if overridden else ""))

    def _set_control(self, key, value):
        self.controls[key] = value
        if key in self.slider_widgets:
            sl, val, fmt = self.slider_widgets[key]
            self._paint_value(key, val, fmt(value))
        elif key in self.knob_widgets:
            self._paint_value(key, self.knob_widgets[key][1], _fmt_pct(value))
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(350, self._save_controls)

    def _save_controls(self):
        self._save_job = None
        z_live.save_controls(self.controls)
        self._update_apply_label()

    def _reset_one(self, key):
        self.controls.pop(key, None)
        self._load_controls_into_widgets()
        self._save_controls()

    def _reset_all(self):
        for key in list(self.controls):
            if key in self.defaults:
                del self.controls[key]
        self._load_controls_into_widgets()
        self._save_controls()

    def _update_apply_label(self):
        live_switches = self.status.get("switches") or {}
        live_settings = self.status.get("settings") or {}
        if not self._watcher_online():
            self.apply_label.config(text="saved \u00b7 applies when watcher starts", fg=MUTED)
            return
        pending = False
        for key, value in self.controls.items():
            if key in live_switches:
                pending |= bool(live_switches[key]) != bool(value)
            elif key in live_settings:
                pending |= abs(float(live_settings[key]["value"]) - float(value)) > 1e-6
        for key in self.defaults:
            if key not in self.controls and key in live_settings:
                pending |= abs(float(live_settings[key]["value"]) - float(self.defaults[key])) > 1e-6
        if pending:
            self.apply_label.config(text=f"\u25cf pending \u2014 next scan{_dots()}", fg=YELLOW)
        else:
            self.apply_label.config(text="\u25cf live", fg=BLUE)

    # ---- activity feed ----
    def _build_feed(self, master):
        outer, body, right = _card(master, "ACTIVITY")
        tk.Checkbutton(
            right.master, text="Porygon Z only", variable=self._z_only, command=self._force_log,
            font=FONT_SM, fg=MUTED, bg=PANEL, selectcolor=PANEL_HI, activebackground=PANEL,
            activeforeground=TEXT, bd=0, highlightthickness=0, cursor="hand2",
        ).pack(side="right")
        wrap = tk.Frame(body, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        self.feed = tk.Text(
            wrap, bg=BG, fg=TEXT, font=FONT_MONO, relief="flat", bd=0, wrap="word",
            padx=10, pady=8, highlightthickness=0, insertbackground=TEXT, state="disabled",
            spacing1=2,
        )
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.feed.yview, style="Z.Vertical.TScrollbar")
        self.feed.configure(yscrollcommand=sb.set)
        self.feed.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (
            ("ts", DIM), ("z", PINK), ("six", YELLOW), ("role", BLUE), ("bad", RED),
            ("twitch", VIOLET), ("other", TEXT),
        ):
            self.feed.tag_configure(tag, foreground=color, lmargin2=84)
        # Discord can't colour a word inline; the panel can. Every name for
        # father in the feed gets painted, however Z dressed it up.
        self.feed.tag_configure(
            "name", foreground=PINK, lmargin2=84, font=("Consolas", 9, "bold"),
        )
        return outer

    def _force_log(self):
        self._log_mtime = 0.0
        self._refresh_log()

    def _refresh_log(self):
        try:
            mtime = os.path.getmtime(LOG_FILE)
        except OSError:
            return
        if mtime == self._log_mtime:
            return
        self._log_mtime = mtime
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()

        today = datetime.date.today()
        self.replies_today = 0
        parsed = []
        for line in lines:
            ts_local, msg = None, line
            if line.startswith("[") and "Z] " in line[:24]:
                raw, msg = line[1:20], line[22:]
                try:
                    ts_local = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=datetime.timezone.utc,
                    ).astimezone()
                except ValueError:
                    pass
            if ts_local and ts_local.date() == today and "Porygon Z replied" in msg:
                self.replies_today += 1
            parsed.append((ts_local, msg))

        if self._z_only.get():
            parsed = [p for p in parsed if "Porygon Z" in p[1]]
        parsed = parsed[-250:]

        at_bottom = self.feed.yview()[1] > 0.98
        self.feed.config(state="normal")
        self.feed.delete("1.0", "end")
        for ts_local, msg in parsed:
            stamp = ""
            if ts_local:
                stamp = ts_local.strftime("%I:%M %p").lstrip("0").rjust(8)
                if ts_local.date() != today:
                    stamp = ts_local.strftime("%b %d")
            msg = _ID_RE.sub(lambda m: self.members.get(m.group(0), m.group(0)), msg)
            low = msg.lower()
            if "\u274c" in msg or "failed" in low or "errored" in low or "gave up" in low:
                tag = "bad"
            elif "callback fired" in low:
                tag = "six"
            elif "porygon z" in low:
                tag = "z"
            elif "granted role" in low or "revoked role" in low:
                tag = "role"
            elif "live" in low or "twitch" in low or "stream" in low:
                tag = "twitch"
            else:
                tag = "other"
            self.feed.insert("end", f"{stamp:<9} ", "ts")
            cursor = 0
            for start, end in porygon_names.find_in_text(msg):
                self.feed.insert("end", msg[cursor:start], tag)
                self.feed.insert("end", msg[start:end], "name")
                cursor = end
            self.feed.insert("end", msg[cursor:] + "\n", tag)
        self.feed.config(state="disabled")
        if at_bottom or not getattr(self, "_feed_scrolled_once", False):
            self.feed.see("end")
            self._feed_scrolled_once = True

    # ---- feature requests ----
    def _build_features(self, master):
        outer, body, self.features_note = _card(master, "FEATURE REQUESTS · !feature")
        wrap = tk.Frame(body, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        # Fixed height: the card is a collapsed list, not the main view —
        # it scrolls rather than pushing the feed off the window.
        self.features_canvas = tk.Canvas(wrap, bg=PANEL, highlightthickness=0, height=136)
        sb = ttk.Scrollbar(
            wrap, orient="vertical", command=self.features_canvas.yview, style="Z.Vertical.TScrollbar",
        )
        self.features_inner = tk.Frame(self.features_canvas, bg=PANEL)
        self.features_inner.bind(
            "<Configure>",
            lambda e: self.features_canvas.configure(scrollregion=self.features_canvas.bbox("all")),
        )
        win = self.features_canvas.create_window((0, 0), window=self.features_inner, anchor="nw")
        self.features_canvas.configure(yscrollcommand=sb.set)
        self.features_canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._features: list[dict] = []
        self._feature_details: list[tk.Label] = []
        self._features_width = 900

        def _resize(e):
            self.features_canvas.itemconfigure(win, width=e.width)
            self._features_width = e.width
            for label in self._feature_details:
                label.config(wraplength=max(200, e.width - 70))
        self.features_canvas.bind("<Configure>", _resize)

        def _wheel(e):
            self.features_canvas.yview_scroll(int(-e.delta / 120), "units")
        self.features_canvas.bind("<Enter>", lambda e: self.features_canvas.bind_all("<MouseWheel>", _wheel))
        self.features_canvas.bind("<Leave>", lambda e: self.features_canvas.unbind_all("<MouseWheel>"))
        return outer

    # ---- manual message ----
    def _build_manual(self, master):
        outer, body, self.manual_note = _card(master, "MANUAL MESSAGE")
        self._manual_channels: list[tuple[str, str]] = []  # (id, "#name"), refreshed live

        row1 = tk.Frame(body, bg=PANEL)
        row1.pack(fill="x")

        tk.Label(row1, text="Speaks as", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self._manual_bot = tk.StringVar(value="z")
        for value, label in (("z", "Porygon Z"), ("porygon", "Porygon")):
            tk.Radiobutton(
                row1, text=label, value=value, variable=self._manual_bot,
                font=FONT, fg=TEXT, bg=PANEL, selectcolor=PANEL_HI,
                activebackground=PANEL, activeforeground=TEXT, highlightthickness=0,
            ).pack(side="left", padx=(8, 0))

        self.manual_channel_label = tk.Label(row1, text="  Channel", font=FONT, fg=MUTED, bg=PANEL)
        self.manual_channel_label.pack(side="left", padx=(16, 4))
        self._manual_channel_by_label: dict[str, str] = {}
        self.manual_channel_combo = ttk.Combobox(row1, width=28, state="readonly", font=FONT)
        self.manual_channel_combo.pack(side="left")
        self.manual_channel_combo.bind("<<ComboboxSelected>>", self._on_manual_channel_picked)
        self.manual_refresh_btn = _button(row1, "↻", self.refresh_manual_channels, color=PANEL_HI)
        self.manual_refresh_btn.pack(side="left", padx=(4, 0))

        row2 = tk.Frame(body, bg=PANEL)
        row2.pack(fill="x", pady=(8, 0))
        self.manual_message_id_label = tk.Label(row2, text="Reply to message ID", font=FONT, fg=MUTED, bg=PANEL)
        self.manual_message_id_label.pack(side="left")
        self.manual_message_id = tk.Entry(
            row2, font=FONT_MONO, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
            relief="flat", width=24,
        )
        self.manual_message_id.pack(side="left", padx=(8, 0), ipady=3)
        self.manual_message_id.bind("<KeyRelease>", self._on_manual_message_id_typed)
        self.manual_message_id.bind("<FocusOut>", lambda e: self._resolve_manual_channel_from_message())
        self.manual_mode_note = tk.Label(row2, text="", font=FONT_SM, fg=DIM, bg=PANEL)
        self.manual_mode_note.pack(side="left", padx=(8, 0))
        self._manual_mode_default_note = "blank = post a fresh message in the channel instead"
        self.manual_mode_note.config(text=f"({self._manual_mode_default_note})")
        self._manual_resolve_job = None
        self._manual_resolving = False

        tk.Label(body, text="What it should say", font=FONT, fg=MUTED, bg=PANEL, anchor="w").pack(
            fill="x", pady=(10, 2),
        )
        self.manual_prompt = tk.Text(
            body, height=3, font=FONT, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
            relief="flat", wrap="word", padx=8, pady=6,
        )
        self.manual_prompt.pack(fill="x")
        tk.Label(
            body, text="tip: just \":emote_name:\" (with the message id above) reacts instead of replying",
            font=FONT_SM, fg=DIM, bg=PANEL, anchor="w",
        ).pack(fill="x", pady=(2, 0))

        self._manual_about_porygon = tk.BooleanVar(value=False)
        row3 = tk.Frame(body, bg=PANEL)
        row3.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(
            row3, text="This message is about/for Porygon (the other bot)",
            variable=self._manual_about_porygon, font=FONT_SM, fg=MUTED, bg=PANEL,
            selectcolor=PANEL_HI, activebackground=PANEL, activeforeground=TEXT,
            highlightthickness=0,
        ).pack(side="left")
        self.manual_send_btn = _button(row3, "Send", self.send_manual, color=PINK_DEEP)
        self.manual_send_btn.pack(side="right")
        self.manual_status = tk.Label(row3, text="", font=FONT_SM, fg=MUTED, bg=PANEL, anchor="e")
        self.manual_status.pack(side="right", padx=(0, 12), fill="x", expand=True)
        return outer

    def refresh_manual_channels(self):
        self.manual_note.config(text="loading channels…", fg=MUTED)

        def work():
            try:
                channels = sorted(_fetch_text_channels(), key=lambda c: c[1].lower())
                self.after(0, lambda: self._manual_channels_loaded(channels))
            except Exception as e:
                self.after(0, lambda: self.manual_note.config(text=f"channel list failed ({e})", fg=YELLOW))

        threading.Thread(target=work, daemon=True).start()

    def _manual_channels_loaded(self, channels: list[tuple[str, str]]):
        self._manual_channels = channels
        self._manual_channel_by_label = {label: cid for cid, label in channels}
        current = self.manual_channel_combo.get()
        self.manual_channel_combo["values"] = [label for _, label in channels]
        if current in self._manual_channel_by_label:
            self.manual_channel_combo.set(current)
        elif channels:
            self.manual_channel_combo.current(0)
        self.manual_note.config(text=f"{len(channels)} channels", fg=MUTED)

    # ---- manual message: channel vs. message-id are mutually exclusive ----
    def _set_manual_channel_active(self, active: bool):
        self.manual_channel_combo.config(state="readonly" if active else "disabled")
        self.manual_channel_label.config(fg=MUTED if active else DIM)
        self.manual_refresh_btn.config(state="normal" if active else "disabled")

    def _set_manual_message_id_active(self, active: bool):
        """Grayed out means grayed OUT, not just dim: a stale id left behind
        while this field is inactive would otherwise quietly still be what
        gets sent, which is exactly the mismatch between what's visible and
        what fires that graying it out is supposed to rule out."""
        self.manual_message_id.config(state="normal")
        if not active:
            self.manual_message_id.delete(0, "end")
        self.manual_message_id.config(
            state="normal" if active else "disabled",
            fg=TEXT if active else DIM,
            bg=PANEL_HI if active else PANEL,
        )
        self.manual_message_id_label.config(fg=MUTED if active else DIM)

    def _on_manual_channel_picked(self, event=None):
        """Fires only on a real user pick from the dropdown list — never on
        the programmatic .set()/.current() calls _manual_channels_loaded
        makes while populating it — so this is a deliberate "post fresh
        here" choice, and any message id typed in for a reply no longer
        applies."""
        self._set_manual_message_id_active(False)
        self.manual_mode_note.config(text=f"({self._manual_mode_default_note})", fg=DIM)

    def _on_manual_message_id_typed(self, event=None):
        has_id = bool(self.manual_message_id.get().strip())
        self._set_manual_channel_active(not has_id)
        if not has_id:
            self.manual_mode_note.config(text=f"({self._manual_mode_default_note})", fg=DIM)
        elif self.manual_mode_note.cget("text") == f"({self._manual_mode_default_note})":
            self.manual_mode_note.config(text="", fg=DIM)
        # Debounced — resolve once typing actually pauses, not on every keystroke.
        if self._manual_resolve_job:
            self.after_cancel(self._manual_resolve_job)
        self._manual_resolve_job = self.after(700, self._resolve_manual_channel_from_message) if has_id else None

    def _resolve_manual_channel_from_message(self):
        """The whole point of graying the channel picker out is that the
        user shouldn't also have to keep it pointed at the right channel by
        hand — so once a message id is typed, find that channel for them.
        Tries whichever channel is already selected first (the common case:
        they were just looking at it), then falls back to checking every
        configured channel."""
        self._manual_resolve_job = None
        message_id = self.manual_message_id.get().strip()
        if not message_id or not message_id.isdigit():
            return
        current_id = self._manual_channel_by_label.get(self.manual_channel_combo.get())
        channels = sorted(self._manual_channels, key=lambda c: c[0] != current_id) if current_id else list(self._manual_channels)
        self.manual_mode_note.config(text="finding its channel…", fg=MUTED)
        self._manual_resolving = True

        def work():
            token = os.environ.get("DISCORD_BOT_TOKEN")
            found = None
            for cid, label in channels:
                try:
                    if discord_roles.get_message(cid, message_id, token):
                        found = (cid, label)
                        break
                except Exception:
                    continue
            self.after(0, lambda: self._manual_channel_resolved(message_id, found))

        threading.Thread(target=work, daemon=True).start()

    def _manual_channel_resolved(self, for_message_id: str, found: Optional[tuple]):
        self._manual_resolving = False
        # The field may have been edited or cleared while the search ran.
        if self.manual_message_id.get().strip() != for_message_id:
            return
        if found:
            _, label = found
            self.manual_channel_combo.set(label)
            self.manual_mode_note.config(text=f"in {label}", fg=BLUE)
        else:
            self.manual_mode_note.config(text="not found in any channel", fg=YELLOW)

    def send_manual(self):
        message_id = self.manual_message_id.get().strip()
        if message_id and self._manual_resolving:
            self.manual_status.config(text="still finding that message's channel — hang on", fg=YELLOW)
            return
        label = self.manual_channel_combo.get()
        channel_id = self._manual_channel_by_label.get(label)
        if not channel_id:
            self.manual_status.config(text="pick a channel first", fg=YELLOW)
            return
        prompt = self.manual_prompt.get("1.0", "end").strip()
        if not prompt:
            self.manual_status.config(text="say what it should say", fg=YELLOW)
            return
        bot = self._manual_bot.get()
        about_porygon = self._manual_about_porygon.get()
        channel_name = label.lstrip("#")

        self.manual_send_btn.config(state="disabled")
        self.manual_status.config(text="sending…", fg=MUTED)

        def work():
            try:
                text = self._compose_and_post_manual(
                    bot, channel_id, channel_name, message_id, prompt, about_porygon,
                )
                self.after(0, lambda: self._manual_done(True, text))
            except Exception as e:
                self.after(0, lambda: self._manual_done(False, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _react_manual(
        self, bot: str, channel_id: str, channel_name: str, message_id: str, emote_name: str,
    ) -> str:
        """The prompt box held nothing but `:emote_name:` — react with it
        instead of composing anything. No compose call, no name-recording,
        no git commit: a reaction touches none of the tracked state files."""
        if not message_id:
            raise RuntimeError(f"give a message id to react to — :{emote_name}: has nothing to react on its own")
        emoji = _resolve_custom_emoji(emote_name)
        if not emoji:
            raise RuntimeError(f"no custom emoji named \"{emote_name}\" in this server")
        act_token = porygon_z._z_token() if bot == "z" else os.environ["DISCORD_BOT_TOKEN"]
        if not discord_roles.add_own_reaction(channel_id, message_id, emoji, act_token):
            raise RuntimeError("Discord rejected the reaction — check the log for the response body")
        who = "Porygon Z" if bot == "z" else "Porygon"
        activity_log.log(f"\U0001f5a5️ Manual reaction :{emote_name}: added as {who} (#{channel_name})")
        return f":{emote_name}: reacted"

    def _compose_and_post_manual(
        self, bot: str, channel_id: str, channel_name: str,
        message_id: str, prompt: str, about_porygon: bool,
    ) -> str:
        """Runs off the UI thread. Returns the posted text, or raises with a
        message fit to show directly in the status label."""
        read_token = os.environ["DISCORD_BOT_TOKEN"]

        shortcode = _EMOTE_SHORTCODE_RE.match(prompt)
        if shortcode:
            return self._react_manual(bot, channel_id, channel_name, message_id, shortcode.group(1))

        context = ""
        if message_id:
            msg = discord_roles.get_message(channel_id, message_id, read_token)
            if not msg:
                raise RuntimeError("couldn't find that message id in that channel")
            author = msg.get("author", {})
            who = author.get("global_name") or author.get("username") or "someone"
            context = f"{who}: {msg.get('content') or '(no text)'}"

        if bot == "z":
            reply = z_brain.compose_manual(prompt, context=context, about_porygon=about_porygon)
            if not reply:
                raise RuntimeError("Z had nothing back (API error, or check the log for a truncated-thinking warning)")
            posted_text = porygon_z._for_post(reply)
            act_token = porygon_z._z_token()
            touched = ["z_reply_history.txt", "porygon_names.json"]
            porygon_names.record_from_line(reply)  # father, if the line named him
            if about_porygon:
                porygon_names.record_from_line(reply, subject="porygon")
                touched.append("porygon_sibling_names.json")
            try:
                with open(porygon_z.REPLY_HISTORY_FILE, "a", encoding="utf-8") as f:
                    f.write(reply.replace("\n", " ").strip() + "\n")
            except OSError:
                pass
        else:
            reply = porygon_voice.compose(prompt, context=context)
            if not reply:
                raise RuntimeError("Porygon had nothing back (API error — check the log)")
            posted_text = porygon_voice.for_post(reply)
            act_token = read_token
            touched = ["porygon_names.json"]
            porygon_names.record_from_line(reply)

        if message_id:
            posted_id = discord_roles.post_reply(channel_id, message_id, act_token, posted_text)
        else:
            posted_id = discord_roles.post_message_with_file(channel_id, act_token, posted_text)
        if not posted_id:
            raise RuntimeError("Discord rejected the post — check the log for the response body")

        who = "Porygon Z" if bot == "z" else "Porygon"
        activity_log.log(f"\U0001f5a5️ Manual message sent as {who} (#{channel_name})")
        touched.append("activity.log")
        _git_commit_push(touched, f"Manual {who} message [skip ci]")
        return posted_text

    def _manual_done(self, ok: bool, text: str):
        self.manual_send_btn.config(state="normal")
        if ok:
            preview = text if len(text) <= 140 else text[:139] + "…"
            self.manual_status.config(text=f"posted: {preview}", fg=BLUE)
            self.manual_message_id.config(state="normal")
            self.manual_message_id.delete(0, "end")
            self._set_manual_message_id_active(True)
            self._set_manual_channel_active(True)
            self.manual_mode_note.config(text=f"({self._manual_mode_default_note})", fg=DIM)
            self.manual_prompt.delete("1.0", "end")
        else:
            self.manual_status.config(text=f"failed: {text}", fg=RED)

    def _refresh_features(self):
        """Reload feature_requests.json when it changes on disk — the watcher
        (or a git pull) writes it; the panel only ever reads it."""
        try:
            mtime = os.path.getmtime(FEATURES_FILE)
        except OSError:
            mtime = 0.0
        if mtime == self._features_mtime:
            return
        self._features_mtime = mtime
        self._features = feature_requests.load_requests()
        n = len(self._features)
        self.features_note.config(text=f"{n} request{'' if n == 1 else 's'}", fg=MUTED)
        self._render_features()

    def _toggle_feature(self, req_id: str):
        if req_id in self._features_open:
            self._features_open.discard(req_id)
        else:
            self._features_open.add(req_id)
        self._render_features()

    def _render_features(self):
        for child in self.features_inner.winfo_children():
            child.destroy()
        self._feature_details = []
        if not self._features:
            tk.Label(
                self.features_inner,
                text="Nothing yet — anyone in the server can post “!feature <idea>”.",
                font=FONT_SM, fg=DIM, bg=PANEL, anchor="w",
            ).pack(fill="x", pady=8)
            return

        for req in self._features:
            req_id = req.get("id") or ""
            is_open = req_id in self._features_open
            row = tk.Frame(self.features_inner, bg=PANEL)
            row.pack(fill="x", pady=(0, 2))
            head = tk.Frame(row, bg=PANEL, cursor="hand2")
            head.pack(fill="x")
            arrow = tk.Label(
                head, text="▼" if is_open else "▶", font=FONT_SM,
                fg=PINK if is_open else MUTED, bg=PANEL, width=2,
            )
            arrow.pack(side="left")
            title = tk.Label(
                head, text=req.get("title") or "(untitled)", font=FONT_B,
                fg=PINK if is_open else TEXT, bg=PANEL, anchor="w",
            )
            title.pack(side="left")
            who = self.members.get(req.get("author_id")) or req.get("author_name") or "someone"
            is_father = bool(porygon_names.find_in_text(who))
            if is_father:
                who = porygon_names.funky(who, markdown=False, braces=False)
            when = _rel_time(_parse_utc(req.get("created_at", "")))
            meta = tk.Label(
                head, text=f"{who} · {when}", font=FONT_SM,
                fg=PINK if is_father else DIM, bg=PANEL, anchor="e",
            )
            meta.pack(side="right")
            for w in (head, arrow, title, meta):
                w.bind("<Button-1>", lambda e, r=req_id: self._toggle_feature(r))

            if is_open:
                detail = tk.Label(
                    row, text=req.get("text") or "", font=FONT, fg=TEXT, bg=BG, anchor="w",
                    justify="left", wraplength=max(200, self._features_width - 70), padx=10, pady=7,
                )
                detail.pack(fill="x", padx=(22, 2), pady=(2, 4))
                self._feature_details.append(detail)

    # ---- watcher process ----
    def start_watcher(self):
        if self.pids:
            messagebox.showinfo("Porygon-Z", f"The watcher is already running (pid {', '.join(map(str, self.pids))}).")
            return
        python = sys.executable
        if os.path.basename(python).lower() == "pythonw.exe":
            # The watcher prints live progress; pythonw has no stdout to print to.
            python = os.path.join(os.path.dirname(python), "python.exe")
        subprocess.Popen(
            [python, os.path.join(_HERE, WATCHER_SCRIPT)], cwd=_HERE,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        self.pill.config(text="STARTING", bg=YELLOW)
        self.after(3000, self._poll_procs_once)

    def stop_watcher(self):
        if not self.pids:
            messagebox.showinfo("Porygon-Z", "The watcher isn't running.")
            return
        if not messagebox.askyesno(
            "Stop watcher?",
            "Stop the Porygon watcher? This also stops the quotes bot and Z until you start it again.",
        ):
            return
        for pid in self.pids:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        z_live._update_status(lambda s: s.update(processing=None))
        self.after(500, self._poll_procs_once)

    # ---- polling ----
    def _watcher_online(self) -> bool:
        return time.time() - self.status.get("heartbeat", 0) < HEARTBEAT_FRESH_SECONDS or bool(self._processing())

    def _poll_status(self):
        self.status = z_live.load_status()
        if set(self._roster()) != set(self.member_rows):
            self._rebuild_member_rows()
        self._update_header()
        self.after(600, self._poll_status)

    def _poll_log(self):
        self._refresh_log()
        self.after(2000, self._poll_log)

    def _poll_features(self):
        self._refresh_features()
        self.after(3000, self._poll_features)

    def _poll_procs_once(self):
        def work():
            pids = _watcher_pids()
            self.after(0, lambda: setattr(self, "pids", pids))
        threading.Thread(target=work, daemon=True).start()

    def _poll_procs(self):
        self._poll_procs_once()
        self.after(10000, self._poll_procs)

    def _animate(self):
        self._update_member_rows()
        self._update_header()
        self._update_apply_label()
        self.after(150, self._animate)

    def _update_header(self):
        proc = self._processing()
        online = self._watcher_online()
        paused = bool((self.status.get("switches") or {}).get("paused"))
        if proc:
            pill, color = "THINKING", PINK
            elapsed = int(time.time() - proc.get("since", time.time()))
            scores = proc.get("scores") or []
            mode = " (encouraging)" if proc.get("mode") == "encouraging" else ""
            doing = (
                f"Drafting a {KIND_LABEL.get(proc.get('kind'), proc.get('kind'))} reply{mode} for "
                f"{proc.get('name')} in #{proc.get('channel')}{_dots()}   {elapsed}s"
            )
            if scores:
                doing += f"   \u00b7   {len(scores)} scored, best {max(scores):g}"
        elif online and paused:
            pill, color = "PAUSED", YELLOW
            doing = "Paused \u2014 reading along, answering nothing."
        elif online:
            pill, color = "ONLINE", BLUE
            ago = int(time.time() - self.status.get("heartbeat", 0))
            doing = f"Idle \u2014 watching the server (last scan {ago}s ago)."
        elif self.pids:
            pill, color = "STARTING", YELLOW
            doing = "Watcher is running; waiting for Z's first scan\u2026"
        else:
            pill, color = "OFFLINE", RED
            doing = "Watcher isn't running. Hit Start to bring Z online."
        if self.pill.cget("text") != pill:
            self.pill.config(text=pill, bg=color)
        if self.doing_label.cget("text") != doing:
            self.doing_label.config(text=doing, fg=PINK if proc else MUTED)
        stats = (
            f"{self.status.get('reply_count', '\u2014')} replies all-time\n"
            f"{getattr(self, 'replies_today', 0)} today"
        )
        if self.stats_label.cget("text") != stats:
            self.stats_label.config(text=stats)


if __name__ == "__main__":
    Panel().mainloop()
