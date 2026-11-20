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
import scheduled_polls  # noqa: E402
import z_brain  # noqa: E402
import z_live  # noqa: E402
import z_memory  # noqa: E402
import z_profiles  # noqa: E402

LOG_FILE = os.path.join(_HERE, "activity.log")
FEATURES_FILE = feature_requests.REQUESTS_FILE
MEMBERS_CACHE = os.path.join(_HERE, "z_members_cache.json")
WATCHER_SCRIPT = "quotes_watch_local.py"
ICON_PATH = r"C:\Users\Williwaugh\Desktop\emotes\porygoncuter.png"

HEARTBEAT_FRESH_SECONDS = 90
# A watcher process that's been up this long without a fresh heartbeat isn't
# starting, it's stuck (e.g. its console got paused in QuickEdit Select mode).
WATCHER_STUCK_SECONDS = 180
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
    ("schedule", "!schedule commands", "Reads the availability sheet and files a dated scheduling poll.", False),
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
    "!schedule": "!schedule",
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
    """(channel_id, label) for every text channel and active thread in every
    configured guild, freshest call wins — this is the "live-updated list"
    the manual tool's dropdown is built from, refetched on demand rather than
    cached, since a channel created/renamed minutes ago should show up
    immediately. Threads are labelled "#parent › thread" (falling back to
    just "#thread" if the parent isn't in this same list) since thread names
    alone are often as generic as "Ikkicon 27'" and only make sense next to
    the channel they're hanging off of."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN missing from .env")
    out: list[tuple[str, str]] = []
    for guild_id in discord_roles.configured_guild_ids():
        channels = discord_roles.get_guild_text_channels(guild_id, token)
        names_by_id = {c["id"]: c.get("name", c["id"]) for c in channels}
        for channel in channels:
            name = channel.get("name", channel["id"])
            if channel.get("type") in (10, 11, 12):  # thread
                parent = names_by_id.get(channel.get("parent_id"))
                label = f"#{parent} › {name}" if parent else f"#{name} (thread)"
            else:
                label = f"#{name}"
            out.append((channel["id"], label))
    return out


_MESSAGE_LINK_RE = re.compile(r"channels/\d+/(\d+)/(\d+)")
_ID_PAIR_RE = re.compile(r"^(\d{15,22})[-/ ](\d{15,22})$")


def _parse_message_ref(text: str) -> tuple[Optional[str], str]:
    """(channel_id or None, message_id) from whatever was pasted: a bare
    message id, Discord's Shift+Copy ID "channel-message" pair, or a full
    message link. message_id is "" when there's nothing usable."""
    text = (text or "").strip()
    m = _MESSAGE_LINK_RE.search(text) or _ID_PAIR_RE.match(text)
    if m:
        return m.group(1), m.group(2)
    return None, text if text.isdigit() else ""


def _lookup_message(channel_id: str, message_id: str) -> Optional[dict]:
    """The message, read with the main bot's token or else Z's: either bot
    may be the only one that can see a given channel."""
    tokens = [os.environ.get("DISCORD_BOT_TOKEN")]
    try:
        tokens.append(porygon_z._z_token())
    except Exception:
        pass
    for token in tokens:
        if not token:
            continue
        try:
            msg = discord_roles.get_message(channel_id, message_id, token)
        except Exception:
            msg = None
        if msg:
            return msg
    return None


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
        self.geometry(f"1320x{min(1000, self.winfo_screenheight() - 80)}+20+10")
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
        self._pids_since = time.time()
        self._save_job = None
        self._log_mtime = 0.0
        self._features_mtime = -1.0
        self._features_open: set[str] = set()
        self._z_only = tk.BooleanVar(value=False)

        style.configure("Z.TNotebook", background=BG, borderwidth=0, tabmargins=0,
                        lightcolor=BG, darkcolor=BG, bordercolor=BG)
        style.configure("Z.TNotebook.Tab", background=PANEL, foreground=MUTED, font=FONT_H,
                        padding=(16, 6), borderwidth=0, lightcolor=PANEL, bordercolor=BG)
        style.map("Z.TNotebook.Tab", background=[("selected", PANEL_HI)], foreground=[("selected", PINK)],
                  lightcolor=[("selected", PANEL_HI)])

        self._build_header()
        tabs = ttk.Notebook(self, style="Z.TNotebook")
        tabs.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        body = tk.Frame(tabs, bg=BG, pady=12)
        tabs.add(body, text="Control")
        planner = tk.Frame(tabs, bg=BG, pady=12)
        tabs.add(planner, text="Poll Planner")
        chat = tk.Frame(tabs, bg=BG, pady=12)
        tabs.add(chat, text="Test Chat")
        lab = tk.Frame(tabs, bg=BG, pady=12)
        tabs.add(lab, text="Reply Lab")
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
        self._build_planner(planner)
        self._build_chat(chat)
        self._build_lab(lab)

        self._rebuild_member_rows()
        self._load_controls_into_widgets()
        self.refresh_members()
        self.refresh_manual_channels()
        self._poll_status()
        self._poll_log()
        self._poll_features()
        self._poll_schedule()
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
        self.features_canvas = tk.Canvas(wrap, bg=PANEL, highlightthickness=0, height=96)
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
                command=self._clear_manual_preview,  # a draft in one voice can't post as the other
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
        self.manual_message_id_label = tk.Label(row2, text="Reply to message ID or link", font=FONT, fg=MUTED, bg=PANEL)
        self.manual_message_id_label.pack(side="left")
        self.manual_message_id = tk.Entry(
            row2, font=FONT_MONO, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
            relief="flat", width=34,
        )
        self.manual_message_id.pack(side="left", padx=(8, 0), ipady=3)
        self.manual_message_id.bind("<KeyRelease>", self._on_manual_message_id_typed)
        self.manual_message_id.bind("<KeyRelease>", lambda e: self._note_manual_preview_stale(), add="+")
        self.manual_message_id.bind("<FocusOut>", lambda e: self._resolve_manual_channel_from_message())
        self.manual_mode_note = tk.Label(row2, text="", font=FONT_SM, fg=DIM, bg=PANEL)
        self.manual_mode_note.pack(side="left", padx=(8, 0))
        self._manual_mode_default_note = "blank = post a fresh message in the channel instead"
        self.manual_mode_note.config(text=f"({self._manual_mode_default_note})")
        self._manual_resolve_job = None
        self._manual_resolving = False
        self._manual_search_wanted = False

        # Prompt on the left, the line it wrote on the right. Nothing posts
        # until Send, and Send posts what's in the right-hand box.
        fields = tk.Frame(body, bg=PANEL)
        fields.pack(fill="x", pady=(10, 0))
        fields.columnconfigure(0, weight=1, uniform="manual")
        fields.columnconfigure(1, weight=1, uniform="manual")

        left = tk.Frame(fields, bg=PANEL)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(left, text="What it should say", font=FONT, fg=MUTED, bg=PANEL, anchor="w").pack(
            fill="x", pady=(0, 2),
        )
        self.manual_prompt = tk.Text(
            left, height=3, font=FONT, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
            relief="flat", wrap="word", padx=8, pady=6,
        )
        self.manual_prompt.pack(fill="x")
        self.manual_prompt.bind("<KeyRelease>", lambda e: self._note_manual_preview_stale())
        tk.Label(
            left, text="tip: just \":emote_name:\" (with the message id above) reacts instead of replying",
            font=FONT_SM, fg=DIM, bg=PANEL, anchor="w",
        ).pack(fill="x", pady=(2, 0))

        right = tk.Frame(fields, bg=PANEL)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        preview_head = tk.Frame(right, bg=PANEL)
        preview_head.pack(fill="x", pady=(0, 2))
        tk.Label(preview_head, text="What it will post (editable)", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self.manual_preview_note = tk.Label(preview_head, text="", font=FONT_SM, fg=YELLOW, bg=PANEL)
        self.manual_preview_note.pack(side="right")
        self.manual_preview = self._scrolling_text(
            right, height=4, font=FONT, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
            relief="flat", wrap="word", padx=8, pady=6,
        )
        self.manual_preview.bind("<KeyRelease>", self._on_manual_preview_edited)
        tk.Label(right, text="As posted", font=FONT_SM, fg=DIM, bg=PANEL, anchor="w").pack(fill="x", pady=(4, 0))
        self.manual_styled = self._scrolling_text(
            right, height=2, font=FONT_SM, fg=MUTED, bg=PANEL, relief="flat", wrap="word",
            padx=0, pady=0, state="disabled",
        )
        feedback_row = tk.Frame(right, bg=PANEL)
        feedback_row.pack(fill="x", pady=(6, 0))
        tk.Label(feedback_row, text="Feedback", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self.manual_feedback_btn = _button(feedback_row, "Redo with feedback", self.redo_manual_with_feedback)
        self.manual_feedback_btn.pack(side="right")
        self.manual_feedback = tk.Entry(
            feedback_row, font=FONT, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT, relief="flat",
        )
        self.manual_feedback.pack(side="left", fill="x", expand=True, padx=(8, 6), ipady=3)
        self.manual_feedback.bind("<Return>", lambda e: self.redo_manual_with_feedback())
        self._manual_revisions: list[tuple[str, str]] = []  # (attempt, father's note), oldest first
        self._manual_draft: Optional[dict] = None  # what the preview was written from
        self._manual_styled_text = ""  # exactly what Send posts
        self._manual_styled_for: Optional[str] = None  # the clean text that styling was made from
        self._manual_restyle_job = None

        self._manual_about_porygon = tk.BooleanVar(value=False)
        row3 = tk.Frame(body, bg=PANEL)
        row3.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(
            row3, text="This message is about/for Porygon (the other bot)",
            variable=self._manual_about_porygon, command=self._note_manual_preview_stale,
            font=FONT_SM, fg=MUTED, bg=PANEL,
            selectcolor=PANEL_HI, activebackground=PANEL, activeforeground=TEXT,
            highlightthickness=0,
        ).pack(side="left")
        self.manual_send_btn = _button(row3, "Send", self.send_manual, color=PINK_DEEP)
        self.manual_send_btn.pack(side="right")
        self.manual_preview_btn = _button(row3, "Preview", self.preview_manual)
        self.manual_preview_btn.pack(side="right", padx=(0, 6))
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
        self._planner_channels_loaded(channels)

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
        """A real user pick from the dropdown. With a message id filled in,
        it says which channel that message is in (and stops any automatic
        search from overriding the choice); with none, it's where a fresh
        message goes."""
        if self._manual_resolve_job:
            self.after_cancel(self._manual_resolve_job)
            self._manual_resolve_job = None
        self._manual_resolving = False
        self._manual_search_wanted = False
        _, message_id = _parse_message_ref(self.manual_message_id.get())
        if not message_id:
            self.manual_mode_note.config(text=f"({self._manual_mode_default_note})", fg=DIM)
            return
        label = self.manual_channel_combo.get()
        channel_id = self._manual_channel_by_label.get(label)
        self.manual_mode_note.config(text=f"checking {label}…", fg=MUTED)

        def work():
            found = _lookup_message(channel_id, message_id) if channel_id else None
            def done():
                if _parse_message_ref(self.manual_message_id.get())[1] != message_id \
                        or self.manual_channel_combo.get() != label:
                    return
                if found:
                    self.manual_mode_note.config(text=f"in {label}", fg=BLUE)
                else:
                    self.manual_mode_note.config(text=f"not in {label} (or neither bot can read it)", fg=YELLOW)
            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _manual_select_channel_id(self, channel_id: str) -> str:
        """Point the dropdown at `channel_id`, adding it if the list doesn't
        have it (an archived thread, say). Returns its label."""
        for cid, label in self._manual_channels:
            if cid == channel_id:
                self.manual_channel_combo.set(label)
                return label
        label = f"#{channel_id} (from link)"
        self._manual_channels.append((channel_id, label))
        self._manual_channel_by_label[label] = channel_id
        self.manual_channel_combo["values"] = [lb for _, lb in self._manual_channels]
        self.manual_channel_combo.set(label)
        return label

    def _on_manual_message_id_typed(self, event=None):
        link_channel, message_id = _parse_message_ref(self.manual_message_id.get())
        has_id = bool(message_id)
        if link_channel:
            # A link or channel-message pair already says where it lives.
            if self._manual_resolve_job:
                self.after_cancel(self._manual_resolve_job)
                self._manual_resolve_job = None
            label = self._manual_select_channel_id(link_channel)
            self.manual_mode_note.config(text=f"in {label}", fg=BLUE)
            return
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
        link_channel, message_id = _parse_message_ref(self.manual_message_id.get())
        if not message_id or link_channel:
            return
        current_id = self._manual_channel_by_label.get(self.manual_channel_combo.get())
        channels = sorted(self._manual_channels, key=lambda c: c[0] != current_id) if current_id else list(self._manual_channels)
        self.manual_mode_note.config(text="finding its channel…", fg=MUTED)
        self._manual_resolving = True
        self._manual_search_wanted = True

        def work():
            found = None
            for cid, label in channels:
                if _lookup_message(cid, message_id):
                    found = (cid, label)
                    break
            self.after(0, lambda: self._manual_channel_resolved(message_id, found))

        threading.Thread(target=work, daemon=True).start()

    def _manual_channel_resolved(self, for_message_id: str, found: Optional[tuple]):
        self._manual_resolving = False
        # The field may have been edited or cleared while the search ran.
        if _parse_message_ref(self.manual_message_id.get())[1] != for_message_id or not self._manual_search_wanted:
            return
        if found:
            _, label = found
            self.manual_channel_combo.set(label)
            self.manual_mode_note.config(text=f"in {label}", fg=BLUE)
        else:
            self.manual_mode_note.config(
                text="couldn't find it: pick its channel above, or paste the message link", fg=YELLOW)

    def _scrolling_text(self, master, **options) -> tk.Text:
        """A Text with its own scrollbar, packed into `master`. A long line
        scrolls inside the box instead of growing the card and squeezing the
        rest of the tab."""
        wrap = tk.Frame(master, bg=PANEL)
        wrap.pack(fill="x")
        text = tk.Text(wrap, **options)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=text.yview, style="Z.Vertical.TScrollbar")
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        return text

    def _manual_target(self) -> Optional[tuple[str, str, str]]:
        """(channel_id, channel_name, message_id) from the form, or None after
        saying what's missing."""
        _, message_id = _parse_message_ref(self.manual_message_id.get())
        if self.manual_message_id.get().strip() and not message_id:
            self.manual_status.config(text="that isn't a message id or link", fg=YELLOW)
            return None
        if message_id and self._manual_resolving:
            self.manual_status.config(text="still finding that message's channel — hang on", fg=YELLOW)
            return None
        label = self.manual_channel_combo.get()
        channel_id = self._manual_channel_by_label.get(label)
        if not channel_id:
            self.manual_status.config(text="pick a channel first", fg=YELLOW)
            return None
        return channel_id, label.lstrip("#"), message_id

    def _set_manual_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.manual_preview_btn.config(state=state)
        self.manual_feedback_btn.config(state=state)
        self.manual_send_btn.config(state=state)

    def redo_manual_with_feedback(self):
        """Send the line in the box back with father's note. The box's current
        text is what the note is about, hand edits included."""
        feedback = self.manual_feedback.get().strip()
        if not feedback:
            self.manual_status.config(text="type your feedback first", fg=YELLOW)
            return
        line = self.manual_preview.get("1.0", "end").strip()
        if not line:
            self.manual_status.config(text="nothing to give feedback on — hit Preview first", fg=YELLOW)
            return
        self.preview_manual(revisions=[*self._manual_revisions, (line, feedback)])

    def preview_manual(self, revisions: Optional[list[tuple[str, str]]] = None):
        """Write the line and show it, posting nothing. Costs one compose call
        each time, and a preview that's thrown away leaves no trace: names and
        reply history only get recorded once a line is actually sent. Plain
        Preview starts over; `revisions` carries the feedback rounds so far."""
        revisions = revisions or []
        target = self._manual_target()
        if not target:
            return
        channel_id, _, message_id = target
        prompt = self.manual_prompt.get("1.0", "end").strip()
        if not prompt:
            self.manual_status.config(text="say what it should say", fg=YELLOW)
            return
        if _EMOTE_SHORTCODE_RE.match(prompt):
            self.manual_status.config(text="that's a reaction — nothing to preview, just hit Send", fg=YELLOW)
            return
        bot = self._manual_bot.get()
        about_porygon = self._manual_about_porygon.get()
        draft = {
            "prompt": prompt, "bot": bot, "about_porygon": about_porygon, "message_id": message_id,
            "revisions": revisions,
        }

        self._set_manual_busy(True)
        self.manual_status.config(text="writing…", fg=MUTED)

        def work():
            try:
                reply = self._compose_manual(bot, channel_id, message_id, prompt, about_porygon, revisions)
                self.after(0, lambda: self._manual_previewed(draft, reply))
            except Exception as e:
                error = str(e)
                self.after(0, lambda: self._manual_preview_failed(error))

        threading.Thread(target=work, daemon=True).start()

    def _manual_previewed(self, draft: dict, reply: str):
        self._set_manual_busy(False)
        if draft["bot"] != self._manual_bot.get():
            # Speaker switched while it was writing: this line is in the wrong voice.
            self.manual_status.config(text="speaker changed while writing — hit Preview again", fg=YELLOW)
            return
        self._manual_draft = draft
        self._manual_revisions = draft["revisions"]
        self.manual_feedback.delete(0, "end")
        self.manual_preview.delete("1.0", "end")
        self.manual_preview.insert("1.0", reply)
        self._restyle_manual_preview()
        self._note_manual_preview_stale()
        self.manual_status.config(text="not posted yet — edit it, give feedback, Preview again, or Send", fg=BLUE)

    def _manual_preview_failed(self, error: str):
        self._set_manual_busy(False)
        self.manual_status.config(text=f"failed: {error}", fg=RED)

    def _on_manual_preview_edited(self, event=None):
        if self._manual_restyle_job:
            self.after_cancel(self._manual_restyle_job)
        self._manual_restyle_job = self.after(400, self._restyle_manual_preview)

    def _restyle_manual_preview(self):
        """Z's styling is random, so it's rolled once per version of the text
        and that exact roll is what Send posts — the glitched line on screen
        is the glitched line that lands."""
        self._manual_restyle_job = None
        text = self.manual_preview.get("1.0", "end").strip()
        if text == self._manual_styled_for:
            return
        bot = self._manual_bot.get()
        self._manual_styled_for = text
        if not text:
            self._manual_styled_text = ""
        elif bot == "z":
            self._manual_styled_text = porygon_z._for_post(text)
        else:
            self._manual_styled_text = porygon_voice.for_post(text)
        self._show_manual_styled(self._manual_styled_text)

    def _show_manual_styled(self, text: str):
        self.manual_styled.config(state="normal")
        self.manual_styled.delete("1.0", "end")
        self.manual_styled.insert("1.0", text)
        self.manual_styled.config(state="disabled")

    def _note_manual_preview_stale(self):
        """The preview is the line that posts, so it can't quietly go out of
        step with the form — say so when the ask has moved on from it."""
        draft = self._manual_draft
        stale = bool(draft) and (
            draft["prompt"] != self.manual_prompt.get("1.0", "end").strip()
            or draft["about_porygon"] != self._manual_about_porygon.get()
            or draft["message_id"] != _parse_message_ref(self.manual_message_id.get())[1]
        )
        self.manual_preview_note.config(text="form changed since this was written" if stale else "")

    def _clear_manual_preview(self):
        if self._manual_restyle_job:
            self.after_cancel(self._manual_restyle_job)
            self._manual_restyle_job = None
        self._manual_draft = None
        self._manual_revisions = []
        self._manual_styled_text = ""
        self._manual_styled_for = None
        self.manual_preview.delete("1.0", "end")
        self.manual_feedback.delete(0, "end")
        self._show_manual_styled("")
        self.manual_preview_note.config(text="")

    def send_manual(self):
        target = self._manual_target()
        if not target:
            return
        channel_id, channel_name, message_id = target
        prompt = self.manual_prompt.get("1.0", "end").strip()
        bot = self._manual_bot.get()
        shortcode = _EMOTE_SHORTCODE_RE.match(prompt)

        if shortcode:
            emote_name = shortcode.group(1)
            action = lambda: self._react_manual(bot, channel_id, channel_name, message_id, emote_name)
        else:
            self._restyle_manual_preview()  # an edit still inside the debounce window
            text = self.manual_preview.get("1.0", "end").strip()
            if not text:
                self.manual_status.config(text="hit Preview first — nothing to send yet", fg=YELLOW)
                return
            styled = self._manual_styled_text
            draft = self._manual_draft
            about_porygon = draft["about_porygon"] if draft else self._manual_about_porygon.get()
            action = lambda: self._post_manual(bot, channel_id, channel_name, message_id, text, styled, about_porygon)

        self._set_manual_busy(True)
        self.manual_status.config(text="sending…", fg=MUTED)

        def work():
            try:
                posted = action()
                self.after(0, lambda: self._manual_done(True, posted))
            except Exception as e:
                error = str(e)
                self.after(0, lambda: self._manual_done(False, error))

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

    def _compose_manual(
        self, bot: str, channel_id: str, message_id: str, prompt: str, about_porygon: bool,
        revisions: list[tuple[str, str]],
    ) -> str:
        """Runs off the UI thread. Returns the clean line (no styling), or
        raises with a message fit to show directly in the status label."""
        context = ""
        if message_id:
            msg = _lookup_message(channel_id, message_id)
            if not msg:
                raise RuntimeError("couldn't find that message in that channel: pick its channel or paste the message link")
            author = msg.get("author", {})
            who = author.get("global_name") or author.get("username") or "someone"
            context = f"{who}: {msg.get('content') or '(no text)'}"

        if bot == "z":
            reply = z_brain.compose_manual(prompt, context=context, about_porygon=about_porygon, revisions=revisions)
            if not reply:
                raise RuntimeError("Z had nothing back (API error, or check the log for a truncated-thinking warning)")
        else:
            reply = porygon_voice.compose(prompt, context=context, revisions=revisions)
            if not reply:
                raise RuntimeError("Porygon had nothing back (API error — check the log)")
        return reply

    def _post_manual(
        self, bot: str, channel_id: str, channel_name: str, message_id: str,
        reply: str, posted_text: str, about_porygon: bool,
    ) -> str:
        """Runs off the UI thread. Posts the previewed line, then records it —
        recording waits for the post so a rejected line isn't remembered as
        said. Returns the posted text, or raises like _compose_manual."""
        act_token = porygon_z._z_token() if bot == "z" else os.environ["DISCORD_BOT_TOKEN"]
        if message_id:
            posted_id = discord_roles.post_reply(channel_id, message_id, act_token, posted_text)
        else:
            posted_id = discord_roles.post_message_with_file(channel_id, act_token, posted_text)
        if not posted_id:
            raise RuntimeError("Discord rejected the post — check the log for the response body")

        touched = ["porygon_names.json"]
        porygon_names.record_from_line(reply)  # father, if the line named him
        if bot == "z":
            touched.append("z_reply_history.txt")
            if about_porygon:
                porygon_names.record_from_line(reply, subject="porygon")
                touched.append("porygon_sibling_names.json")
            try:
                with open(porygon_z.REPLY_HISTORY_FILE, "a", encoding="utf-8") as f:
                    f.write(reply.replace("\n", " ").strip() + "\n")
            except OSError:
                pass

        who = "Porygon Z" if bot == "z" else "Porygon"
        activity_log.log(f"\U0001f5a5️ Manual message sent as {who} (#{channel_name})")
        touched.append("activity.log")
        _git_commit_push(touched, f"Manual {who} message [skip ci]")
        return posted_text

    def _manual_done(self, ok: bool, text: str):
        self._set_manual_busy(False)
        if ok:
            preview = text if len(text) <= 140 else text[:139] + "…"
            self.manual_status.config(text=f"posted: {preview}", fg=BLUE)
            self.manual_message_id.config(state="normal")
            self.manual_message_id.delete(0, "end")
            self._set_manual_message_id_active(True)
            self._set_manual_channel_active(True)
            self.manual_mode_note.config(text=f"({self._manual_mode_default_note})", fg=DIM)
            self.manual_prompt.delete("1.0", "end")
            self._clear_manual_preview()
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

    # ---- poll planner tab ----
    # Draft a poll with Claude, edit it, and schedule it for a channel. The
    # panel only writes scheduled_polls.json; the watcher posts each one when
    # its time comes (scheduled_polls.post_due), so the panel can be closed.
    # ---- test chat ----
    # A private sandbox for Z: same compose -> score pipeline as a real
    # summon (persona, speech-training clips, running style note, rubric
    # judging), but nothing posts to Discord and nothing is recorded — no
    # reply history, no style-note update, no names. The chat below is the
    # only "channel" he sees.
    _CHAT_SPEECH_MODES = ("Normal", "Always", "Off")

    # ---- Reply Lab: re-roll anything Z has replied to, nothing posts ----------
    _LAB_PAGES = 3  # pages of 100 messages read per channel when scanning

    def _build_lab(self, master):
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        outer, body, self.lab_note = _card(master, "REPLY LAB (RE-ROLLS NEVER POST)")
        outer.grid(row=0, column=0, sticky="nsew")
        self._lab_items: dict[str, dict] = {}
        self._lab_busy = False

        top = tk.Frame(body, bg=PANEL)
        top.pack(fill="x")
        self.lab_scan_btn = _button(top, "Load Z's replies from the server", self._lab_scan)
        self.lab_scan_btn.pack(side="left")
        tk.Label(top, text="Filter", font=FONT, fg=MUTED, bg=PANEL).pack(side="left", padx=(16, 4))
        self.lab_filter = tk.Entry(top, font=FONT, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
                                   relief="flat", width=28)
        self.lab_filter.pack(side="left", ipady=3)
        self.lab_filter.bind("<KeyRelease>", lambda e: self._lab_fill())
        self._lab_show_all = tk.BooleanVar(value=True)
        tk.Checkbutton(
            top, text="Show all drafts + scores", variable=self._lab_show_all,
            font=FONT, fg=TEXT, bg=PANEL, selectcolor=PANEL_HI,
            activebackground=PANEL, activeforeground=TEXT, highlightthickness=0,
        ).pack(side="left", padx=(16, 0))
        self.lab_roll_btn = _button(top, "Re-roll selected", self._lab_reroll, color=PINK_DEEP)
        self.lab_roll_btn.pack(side="right")

        style = ttk.Style(self)
        style.configure("Lab.Treeview", background=PANEL_HI, fieldbackground=PANEL_HI, foreground=TEXT,
                        font=FONT, rowheight=22, borderwidth=0)
        style.configure("Lab.Treeview.Heading", background=PANEL, foreground=MUTED, font=FONT_B)
        style.map("Lab.Treeview", background=[("selected", PINK_DEEP)])

        split = tk.PanedWindow(body, orient="vertical", bg=PANEL, sashwidth=6, bd=0)
        split.pack(fill="both", expand=True, pady=(10, 0))
        list_frame = tk.Frame(split, bg=PANEL)
        cols = ("when", "channel", "who", "message", "z")
        self.lab_tree = ttk.Treeview(list_frame, columns=cols, show="headings", style="Lab.Treeview",
                                     selectmode="browse", height=10)
        for col, head, width in (("when", "When", 105), ("channel", "Channel", 150), ("who", "Who", 110),
                                 ("message", "They said", 380), ("z", "Z replied", 380)):
            self.lab_tree.heading(col, text=head)
            self.lab_tree.column(col, width=width, stretch=col in ("message", "z"))
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.lab_tree.yview,
                           style="Z.Vertical.TScrollbar")
        self.lab_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.lab_tree.pack(side="left", fill="both", expand=True)
        self.lab_tree.bind("<<TreeviewSelect>>", lambda e: self._lab_show_selected())
        self.lab_tree.bind("<Double-1>", lambda e: self._lab_reroll())
        split.add(list_frame, minsize=140, height=260)

        log_frame = tk.Frame(split, bg=PANEL)
        self.lab_log = tk.Text(log_frame, font=("Segoe UI", 11), fg=TEXT, bg=PANEL_HI, relief="flat",
                               wrap="word", padx=12, pady=10, state="disabled", cursor="arrow")
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.lab_log.yview,
                            style="Z.Vertical.TScrollbar")
        self.lab_log.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.lab_log.pack(side="left", fill="both", expand=True)
        for tag, kw in (("you", dict(foreground=YELLOW, font=("Segoe UI Semibold", 11))),
                        ("z", dict(foreground=PINK, font=("Segoe UI Semibold", 11))),
                        ("meta", dict(foreground=DIM, font=FONT_SM)),
                        ("draft", dict(foreground=MUTED, font=FONT, lmargin1=24, lmargin2=24)),
                        ("err", dict(foreground=RED))):
            self.lab_log.tag_configure(tag, **kw)
        split.add(log_frame, minsize=160)
        tk.Label(body, text="Pick a reply and hit Re-roll (or double-click). It drafts against the same "
                 "conversation Z saw, with his current personality. Nothing is posted or recorded.",
                 font=FONT_SM, fg=DIM, bg=PANEL, anchor="w").pack(fill="x", pady=(4, 0))

    def _lab_write(self, text: str, tag: str = "") -> None:
        self.lab_log.config(state="normal")
        self.lab_log.insert("end", text, tag)
        self.lab_log.config(state="disabled")
        self.lab_log.see("end")

    def _lab_scan(self):
        if self._lab_busy:
            return
        self._lab_busy = True
        self.lab_scan_btn.config(state="disabled")
        self.lab_note.config(text="reading the server… (a minute or so)", fg=YELLOW)

        def work():
            try:
                items = self._lab_collect()
                self.after(0, lambda: self._lab_loaded(items, None))
            except Exception as e:
                err = str(e) or type(e).__name__
                self.after(0, lambda: self._lab_loaded(None, err))

        threading.Thread(target=work, daemon=True).start()

    @classmethod
    def _lab_collect(cls) -> list[dict]:
        """Every message Z posted as a reply in the last few pages of every
        channel, paired with the message it answered."""
        token = os.environ["DISCORD_BOT_TOKEN"]
        z_token = porygon_z._z_token()
        z_id = porygon_z._z_user_id(z_token, "")
        if not z_id:
            raise RuntimeError("couldn't work out Z's user id")
        found = []
        for cid, label in _fetch_text_channels():
            before = None
            for _ in range(cls._LAB_PAGES):
                try:
                    page = discord_roles.get_channel_messages(cid, token, before=before, limit=100) or []
                except Exception:
                    page = []
                if not page:
                    break
                for m in page:
                    parent = m.get("referenced_message")
                    if (m.get("author") or {}).get("id") == z_id and parent:
                        found.append({"channel_id": cid, "channel": label, "z": m, "target": parent})
                before = min(page, key=lambda x: int(x["id"]))["id"]
                if len(page) < 100:
                    break
        found.sort(key=lambda it: int(it["z"]["id"]), reverse=True)
        return found

    def _lab_loaded(self, items, error):
        self._lab_busy = False
        self.lab_scan_btn.config(state="normal")
        if error:
            self.lab_note.config(text=f"scan failed: {error}", fg=RED)
            return
        self._lab_all = items
        self._lab_fill()
        self.lab_note.config(text=f"{len(items)} replies found", fg=MUTED)

    def _lab_fill(self):
        items = getattr(self, "_lab_all", [])
        needle = self.lab_filter.get().strip().lower()
        self.lab_tree.delete(*self.lab_tree.get_children())
        self._lab_items = {}
        for it in items:
            t, z = it["target"], it["z"]
            who = (t.get("author") or {}).get("global_name") or (t.get("author") or {}).get("username") or "?"
            said = " ".join((t.get("content") or "").split()) or ("[picture]" if z_brain._has_image(t) else "")
            zsaid = " ".join((z.get("content") or "").split())
            row = (time.strftime("%m-%d %H:%M", time.localtime(z_brain._msg_time(z["id"]))),
                   it["channel"], who, said[:200], zsaid[:200])
            if needle and needle not in " ".join(row).lower():
                continue
            iid = self.lab_tree.insert("", "end", values=row)
            self._lab_items[iid] = it

    def _lab_selected(self) -> Optional[dict]:
        sel = self.lab_tree.selection()
        return self._lab_items.get(sel[0]) if sel else None

    def _lab_show_selected(self):
        it = self._lab_selected()
        if not it or self._lab_busy:
            return
        t, z = it["target"], it["z"]
        who = (t.get("author") or {}).get("global_name") or (t.get("author") or {}).get("username") or "?"
        self.lab_log.config(state="normal")
        self.lab_log.delete("1.0", "end")
        self.lab_log.config(state="disabled")
        self._lab_write(f"{who} in {it['channel']}\n", "you")
        self._lab_write((t.get("content") or "").strip() + (" [picture]" if z_brain._has_image(t) else "") + "\n\n")
        self._lab_write("Porygon Z (what was posted)\n", "z")
        self._lab_write((z.get("content") or "").strip() + "\n\n")

    def _lab_reroll(self):
        it = self._lab_selected()
        if not it or self._lab_busy:
            if not it:
                self.lab_note.config(text="pick a reply first", fg=YELLOW)
            return
        self._lab_busy = True
        self.lab_roll_btn.config(state="disabled")
        self.lab_note.config(text="Z is drafting… (a minute or two)", fg=YELLOW)
        self._lab_write("— re-roll —\n", "meta")

        def work():
            try:
                result = self._lab_compose(it)
                self.after(0, lambda: self._lab_done(result, None))
            except Exception as e:
                err = str(e) or type(e).__name__
                self.after(0, lambda: self._lab_done(None, err))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _lab_compose(it: dict) -> dict:
        """The live pipeline against the original conversation. Runs in this
        (panel) process, so nothing touches the watcher, the server, reply
        history or the style note."""
        token = os.environ["DISCORD_BOT_TOKEN"]
        # Fresh copy each roll: target_images/_look_at cache onto the dict.
        target = {k: v for k, v in it["target"].items() if k not in ("images", "_seen", "_recent_msgs")}
        channel_name = it["channel"].lstrip("#").split(" › ")[-1]
        context, user_ids = z_brain.build_context(it["channel_id"], target, token)
        recent = porygon_z._load_recent_replies(porygon_z.RECENT_REPLIES_LIMIT)
        ranked = z_brain.compose_scored(
            context, target, channel_name, 0, user_ids, z_brain.COMMAND_MAX_ATTEMPTS,
            recent_replies=recent,
        )
        if not ranked:
            raise RuntimeError("no drafts came back (check the log)")
        banter = z_brain._banter_channels.get(channel_name) if z_brain.is_delicate(channel_name) else None
        return {"ranked": ranked, "images": len(target.get("images") or []), "banter": banter}

    def _lab_done(self, result, error):
        self._lab_busy = False
        self.lab_roll_btn.config(state="normal")
        if error:
            self._lab_write(f"(failed: {error})\n\n", "err")
            self.lab_note.config(text="failed", fg=RED)
            return
        ranked = result["ranked"]
        best = ranked[0]
        score_txt = f"{best['score']:g}" if best["score"] is not None else "?"
        self._lab_write("Porygon Z (re-roll)\n", "z")
        self._lab_write(f"{best['line']}\n")
        extras = []
        if result["images"]:
            extras.append(f"looked at {result['images']} picture(s)")
        if result["banter"] is not None:
            extras.append("vent channel: " + ("banter mode" if result["banter"] else "venting rules"))
        self._lab_write(f"judge {score_txt} · {best['register']} · {best['stage']}"
                        + "".join(f" · {x}" for x in extras) + "\n", "meta")
        if self._lab_show_all.get():
            for r in ranked[1:]:
                st = f"{r['score']:g}" if r["score"] is not None else "?"
                tag = r["register"] + (", punched up" if r["stage"] == "punch-up" else "")
                self._lab_write(f"[{st}] ({tag}) {r['line']}"
                                + (f"  — fix: {r['fix']}" if r.get("fix") else "") + "\n", "draft")
        self._lab_write("\n")
        self.lab_note.config(text=f"best of {len(ranked)} · judge {score_txt}", fg=MUTED)

    def _build_chat(self, master):
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        outer, body, self.chat_note = _card(master, "TALK TO Z (TEST ONLY — NOTHING POSTS)")
        outer.grid(row=0, column=0, sticky="nsew")
        self._chat_history: list[tuple[str, str]] = []  # (speaker, text)
        self._chat_z_replies: list[str] = []
        self._chat_busy = False

        opts = tk.Frame(body, bg=PANEL)
        opts.pack(fill="x")
        tk.Label(opts, text="Speech clips", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self._chat_speech = tk.StringVar(value="Normal")
        for mode in self._CHAT_SPEECH_MODES:
            tk.Radiobutton(
                opts, text=mode, value=mode, variable=self._chat_speech,
                font=FONT, fg=TEXT, bg=PANEL, selectcolor=PANEL_HI,
                activebackground=PANEL, activeforeground=TEXT, highlightthickness=0,
            ).pack(side="left", padx=(8, 0))
        self._chat_show_all = tk.BooleanVar(value=True)
        tk.Checkbutton(
            opts, text="Show all drafts + scores", variable=self._chat_show_all,
            font=FONT, fg=TEXT, bg=PANEL, selectcolor=PANEL_HI,
            activebackground=PANEL, activeforeground=TEXT, highlightthickness=0,
        ).pack(side="left", padx=(12, 0))
        _button(opts, "Clear chat", self._chat_clear, color=PANEL_HI).pack(side="right")

        # Who Z thinks is talking, and whether to lean on what it remembers
        # about them. Personal mode pushes most of the batch onto that
        # person's tagged memories (z_memory); live, it's only a couple of
        # drafts per batch.
        who = tk.Frame(body, bg=PANEL)
        who.pack(fill="x", pady=(8, 0))
        tk.Label(who, text="Speaking as", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self._chat_people: dict[str, str] = {}
        self.chat_person_combo = ttk.Combobox(who, width=34, state="readonly", font=FONT)
        self.chat_person_combo.pack(side="left", padx=(8, 0))
        self._chat_personal = tk.BooleanVar(value=False)
        tk.Checkbutton(
            who, text="Personal mode (callbacks + jabs from memories)", variable=self._chat_personal,
            font=FONT, fg=TEXT, bg=PANEL, selectcolor=PANEL_HI,
            activebackground=PANEL, activeforeground=TEXT, highlightthickness=0,
        ).pack(side="left", padx=(16, 0))
        self.chat_learn_btn = _button(who, "Learn now", self._chat_learn_now, color=PANEL_HI)
        self.chat_learn_btn.pack(side="right")
        self._chat_refresh_people()

        log_frame = tk.Frame(body, bg=PANEL)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.chat_log = tk.Text(
            log_frame, font=("Segoe UI", 11), fg=TEXT, bg=PANEL_HI, relief="flat",
            wrap="word", padx=12, pady=10, state="disabled", cursor="arrow",
        )
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.chat_log.yview,
                               style="Z.Vertical.TScrollbar")
        self.chat_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.chat_log.pack(side="left", fill="both", expand=True)
        self.chat_log.tag_configure("you", foreground=YELLOW, font=("Segoe UI Semibold", 11))
        self.chat_log.tag_configure("z", foreground=PINK, font=("Segoe UI Semibold", 11))
        self.chat_log.tag_configure("meta", foreground=DIM, font=FONT_SM)
        self.chat_log.tag_configure("draft", foreground=MUTED, font=FONT, lmargin1=24, lmargin2=24)
        self.chat_log.tag_configure("err", foreground=RED)

        entry_row = tk.Frame(body, bg=PANEL)
        entry_row.pack(fill="x", pady=(10, 0))
        self.chat_entry = tk.Text(
            entry_row, height=3, font=("Segoe UI", 11), fg=TEXT, bg=PANEL_HI,
            insertbackground=TEXT, relief="flat", wrap="word", padx=8, pady=6,
        )
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", self._chat_on_enter)
        self.chat_send_btn = _button(entry_row, "Send", self._chat_send, color=PINK_DEEP)
        self.chat_send_btn.pack(side="left", padx=(8, 0), fill="y")
        # A picture rides along with the next message, like a Discord attachment.
        self._chat_image_path: Optional[str] = None
        self._chat_thumbs: list = []  # keep PhotoImages alive
        self.chat_image_btn = _button(entry_row, "Picture…", self._chat_pick_image, color=PANEL_HI)
        self.chat_image_btn.pack(side="left", padx=(8, 0), fill="y")
        tk.Label(body, text="Enter sends, Shift+Enter for a new line. Each message costs a real "
                 "compose + score call, same as a !z, and takes a minute or two.", font=FONT_SM, fg=DIM, bg=PANEL,
                 anchor="w").pack(fill="x", pady=(4, 0))

    def _chat_pick_image(self):
        from tkinter import filedialog
        if self._chat_image_path:  # second click clears it
            self._chat_image_path = None
            self.chat_image_btn.config(text="Picture…")
            self.chat_note.config(text="picture removed", fg=MUTED)
            return
        path = filedialog.askopenfilename(
            parent=self, title="Attach a picture for Z",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.webp"), ("All files", "*.*")],
        )
        if path:
            self._chat_image_path = path
            self.chat_image_btn.config(text="✕ " + os.path.basename(path)[:18])
            self.chat_note.config(text="picture attached to your next message", fg=MUTED)

    def _chat_show_thumb(self, path: str) -> None:
        try:
            from PIL import Image, ImageTk
            im = Image.open(path)
            im.thumbnail((220, 220))
            photo = ImageTk.PhotoImage(im)
        except Exception as e:
            self._chat_write(f"[picture: {os.path.basename(path)} ({e})]\n", "meta")
            return
        self._chat_thumbs.append(photo)
        self.chat_log.config(state="normal")
        self.chat_log.image_create("end", image=photo)
        self.chat_log.insert("end", "\n")
        self.chat_log.config(state="disabled")
        self.chat_log.see("end")

    def _chat_on_enter(self, event):
        if event.state & 0x0001:  # Shift held: newline
            return None
        self._chat_send()
        return "break"

    def _chat_write(self, text: str, tag: str = "") -> None:
        self.chat_log.config(state="normal")
        self.chat_log.insert("end", text, tag)
        self.chat_log.config(state="disabled")
        self.chat_log.see("end")

    def _chat_clear(self):
        if self._chat_busy:
            return
        self._chat_history.clear()
        self._chat_z_replies.clear()
        self.chat_log.config(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.config(state="disabled")
        self.chat_note.config(text="", fg=MUTED)

    def _chat_refresh_people(self):
        people = {}
        father = z_brain.FATHER_USER_ID
        counts = {uid: (name, n) for uid, name, n in z_memory.profiled_people()}
        if father:
            name, n = counts.get(father, (self.members.get(father) or "father", 0))
            people[f"{name} (you, {n} memories)"] = father
        for uid, (name, n) in counts.items():
            if uid != father:
                people[f"{name} ({n} memories)"] = uid
        people["tester (no profile)"] = "panel-test"
        current = self.chat_person_combo.get()
        self._chat_people = people
        self.chat_person_combo.config(values=list(people))
        self.chat_person_combo.set(current if current in people else next(iter(people)))

    def _chat_person(self) -> tuple[str, str]:
        """(display name, user id) of whoever Test Chat is speaking as."""
        label = self.chat_person_combo.get()
        uid = self._chat_people.get(label, "panel-test")
        if uid == "panel-test":
            return "tester", uid
        return label.split(" (")[0], uid

    def _chat_name(self) -> str:
        return self._chat_person()[0]

    def _chat_learn_now(self):
        """Run the profile/memory scan right now instead of waiting for the
        12-hour cadence. Reads the server, so it takes a minute or so."""
        if self._chat_busy:
            return
        self.chat_learn_btn.config(state="disabled")
        self.chat_note.config(text="Learning from the server… (a minute or so)", fg=YELLOW)

        def work():
            try:
                before = {uid: n for uid, _, n in z_memory.profiled_people()}
                z_profiles.scan(set(), force=True)
                after = {uid: n for uid, _, n in z_memory.profiled_people()}
                added = sum(max(0, n - before.get(uid, 0)) for uid, n in after.items())
                self.after(0, lambda: self._chat_learned(f"learned {added} new memories", None))
            except Exception as e:
                error = str(e) or type(e).__name__
                self.after(0, lambda: self._chat_learned(None, error))

        threading.Thread(target=work, daemon=True).start()

    def _chat_learned(self, msg, error):
        self.chat_learn_btn.config(state="normal")
        if error:
            self.chat_note.config(text=f"learn failed: {error}", fg=RED)
            return
        self._chat_refresh_people()
        self.chat_note.config(text=msg, fg=MUTED)

    def _chat_send(self):
        text = self.chat_entry.get("1.0", "end").strip()
        image_path = self._chat_image_path
        if (not text and not image_path) or self._chat_busy:
            return
        self.chat_entry.delete("1.0", "end")
        name = self._chat_name()
        self._chat_write(f"{name}\n", "you")
        if image_path:
            self._chat_show_thumb(image_path)
            self._chat_image_path = None
            self.chat_image_btn.config(text="Picture…")
        self._chat_write(f"{text}\n\n" if text else "\n")
        self._chat_history.append((name, text + (" [picture attached]" if image_path else "")))
        self._chat_busy = True
        self.chat_send_btn.config(state="disabled")
        self.chat_note.config(text="Z is drafting… (a minute or two)", fg=YELLOW)

        author_id = self._chat_person()[1]
        history = list(self._chat_history)
        recent = porygon_z._load_recent_replies(porygon_z.RECENT_REPLIES_LIMIT) + self._chat_z_replies[-20:]
        mode = self._chat_speech.get()
        personal_share = 0.6 if self._chat_personal.get() else None

        def work():
            try:
                result = self._chat_compose(history, name, text, author_id, recent, mode, personal_share,
                                            image_path)
                self.after(0, lambda: self._chat_done(result, None))
            except Exception as e:
                error = str(e) or type(e).__name__
                self.after(0, lambda: self._chat_done(None, error))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _chat_compose(history, name, text, author_id, recent, mode, personal_share=None,
                      image_path=None) -> dict:
        """Runs off the UI thread. The panel is its own process, so swapping
        z_brain's speech hook here can't touch the live watcher."""
        lines = [f"{who}: {said}" for who, said in history[:-1][-50:]]
        lines.append(f">>> {name}: {text}" + (" [picture attached]" if image_path else ""))
        context = "\n".join(lines)
        target = {"id": "0", "content": text, "author": {"id": author_id, "username": name, "global_name": name}}
        images = []
        if image_path:
            im = z_brain.image_from_file(image_path)
            if not im:
                raise RuntimeError("couldn't read that picture")
            images.append(im)
        target["images"] = images

        clips: list[str] = []
        original = z_brain._maybe_speech_note
        rate = z_brain._SPEECH_RATE

        def spy():
            note = original()
            clips.append("blend" if "--- source B ---" in note else "clip" if note else "none")
            return note

        z_brain._maybe_speech_note = spy
        z_brain._SPEECH_RATE = {"Always": 1.0, "Off": 0.0}.get(mode, rate)
        try:
            ranked = z_brain.compose_scored(
                context, target, "test-chat", 0, {author_id},
                z_brain.COMMAND_MAX_ATTEMPTS, recent_replies=recent,
                personal_share=personal_share,
            )
        finally:
            z_brain._maybe_speech_note = original
            z_brain._SPEECH_RATE = rate
        if not ranked:
            raise RuntimeError("no drafts came back (API trouble? check the log)")
        used = [c for c in clips if c != "none"]
        clip_txt = f"{len(used)} of {len(clips)} prompts had a clip" if clips else "none"
        return {"ranked": ranked, "clip": clip_txt, "message": text}

    def _chat_done(self, result, error):
        self._chat_busy = False
        self.chat_send_btn.config(state="normal")
        if error:
            self._chat_write(f"(failed: {error})\n\n", "err")
            self.chat_note.config(text="failed", fg=RED)
            return
        ranked = result["ranked"]
        best = ranked[0]
        if self._chat_personal.get():
            # Personal mode answers with the best personal draft; the rest
            # still show below for comparison.
            best = next((r for r in ranked if r.get("personal")), best)
        line, score = best["line"], best["score"]
        self._chat_history.append(("Porygon Z", line))
        self._chat_z_replies.append(line)
        self._chat_write("Porygon Z\n", "z")
        self._chat_write(f"{line}\n")
        score_txt = f"{score:g}" if score is not None else "?"
        punched = sum(1 for r in ranked if r["stage"] == "punch-up")
        self._chat_write(
            f"judge {score_txt} · {best['register']} · {best['stage']}"
            f" · speech: {result['clip']}\n", "meta",
        )
        info = best.get("personal") or next((r["personal"] for r in ranked if r.get("personal")), None)
        if info:
            dug = ", ".join(info.get("tags") or []) or "?"
            angle = f" · {info['angle']}" if info.get("angle") else ""
            self._chat_write(f"memories dug: {dug}{angle}\n", "meta")
            for m in info.get("memories") or []:
                self._chat_write(f"   • {m}\n", "meta")
        elif self._chat_personal.get():
            self._chat_write("(personal mode: nothing remembered about this person yet; try Learn now)\n", "meta")
        self._chat_rating_row(result["message"], line)
        if self._chat_show_all.get() and len(ranked) > 1:
            for r in (r for r in ranked if r is not best):
                st = f"{r['score']:g}" if r["score"] is not None else "?"
                tag = f"{r['register']}" + (", punched up" if r["stage"] == "punch-up" else "")
                self._chat_write(
                    f"[{st}] ({tag}) {r['line']}" + (f"  — fix: {r['fix']}" if r.get("fix") else "") + "\n",
                    "draft",
                )
        self._chat_write("\n")
        self.chat_note.config(
            text=f"best of {len(ranked)} ({punched} punched up) · judge {score_txt}", fg=MUTED,
        )

    def _chat_rating_row(self, message: str, line: str):
        """Your own 1-7 for this reply. Saved into the rubric's calibration
        anchors, so the judge learns your taste over time."""
        row = tk.Frame(self.chat_log, bg=PANEL_HI)
        tk.Label(row, text="your rating:", font=FONT_SM, fg=MUTED, bg=PANEL_HI).pack(side="left")
        why = tk.Entry(row, font=FONT_SM, fg=TEXT, bg=PANEL, insertbackground=TEXT,
                       relief="flat", width=34)
        buttons = []

        def rate(n):
            try:
                z_brain.add_owner_rating(message, line, n, why.get().strip())
            except Exception as e:
                status.config(text=f"save failed: {e}", fg=RED)
                return
            for b in buttons:
                b.destroy()
            why.destroy()
            status.config(text=f"rated {n} — saved to rubric", fg=PINK)

        for n in range(1, 8):
            b = tk.Button(row, text=str(n), command=lambda n=n: rate(n), font=FONT_SM, fg=TEXT,
                          bg=PANEL, activebackground=PINK_DEEP, relief="flat", bd=0,
                          padx=6, pady=1, cursor="hand2")
            b.pack(side="left", padx=(4, 0))
            buttons.append(b)
        tk.Label(row, text="  why (optional)", font=FONT_SM, fg=DIM, bg=PANEL_HI).pack(side="left")
        why.pack(side="left", padx=(4, 0))
        status = tk.Label(row, text="", font=FONT_SM, fg=MUTED, bg=PANEL_HI)
        status.pack(side="left", padx=(8, 0))
        self.chat_log.config(state="normal")
        self.chat_log.window_create("end", window=row)
        self.chat_log.insert("end", "\n")
        self.chat_log.config(state="disabled")
        self.chat_log.see("end")

    def _build_planner(self, master):
        master.columnconfigure(0, weight=1)
        master.columnconfigure(1, weight=0, minsize=430)
        master.rowconfigure(0, weight=1)

        outer, body, self.planner_note = _card(master, "PLAN A POLL")
        outer.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._planner_channel_by_label: dict[str, str] = {}

        row1 = tk.Frame(body, bg=PANEL)
        row1.pack(fill="x")
        tk.Label(row1, text="Channel", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self.planner_channel_combo = ttk.Combobox(row1, width=30, state="readonly", font=FONT)
        self.planner_channel_combo.pack(side="left", padx=(8, 0))
        _button(row1, "↻", self.refresh_manual_channels, color=PANEL_HI).pack(side="left", padx=(4, 0))
        tk.Label(row1, text="  Posts as", font=FONT, fg=MUTED, bg=PANEL).pack(side="left", padx=(16, 0))
        self._planner_bot = tk.StringVar(value="z")
        for value, label in (("z", "Porygon Z"), ("porygon", "Porygon")):
            tk.Radiobutton(
                row1, text=label, value=value, variable=self._planner_bot,
                font=FONT, fg=TEXT, bg=PANEL, selectcolor=PANEL_HI,
                activebackground=PANEL, activeforeground=TEXT, highlightthickness=0,
            ).pack(side="left", padx=(8, 0))

        tk.Label(body, text="What poll do you want? (Claude drafts it — you can edit everything after)",
                 font=FONT, fg=MUTED, bg=PANEL, anchor="w").pack(fill="x", pady=(12, 2))
        self.planner_prompt = tk.Text(
            body, height=3, font=FONT, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
            relief="flat", wrap="word", padx=8, pady=6,
        )
        self.planner_prompt.pack(fill="x")
        row2 = tk.Frame(body, bg=PANEL)
        row2.pack(fill="x", pady=(6, 0))
        self.planner_draft_btn = _button(row2, "✦  Draft with Claude", self.draft_planner_poll)
        self.planner_draft_btn.pack(side="left")
        self.planner_draft_status = tk.Label(row2, text="", font=FONT_SM, fg=MUTED, bg=PANEL, anchor="w")
        self.planner_draft_status.pack(side="left", padx=(10, 0))

        tk.Frame(body, bg=LINE, height=1).pack(fill="x", pady=12)

        tk.Label(body, text="Question", font=FONT, fg=MUTED, bg=PANEL, anchor="w").pack(fill="x")
        self.planner_question = tk.Entry(
            body, font=FONT, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT, relief="flat",
        )
        self.planner_question.pack(fill="x", ipady=4, pady=(2, 0))

        opts_head = tk.Frame(body, bg=PANEL)
        opts_head.pack(fill="x", pady=(10, 2))
        tk.Label(opts_head, text="Options", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        tk.Label(opts_head, text=f"2–{scheduled_polls.MAX_OPTIONS}, up to {scheduled_polls.MAX_OPTION_CHARS} chars each",
                 font=FONT_SM, fg=DIM, bg=PANEL).pack(side="left", padx=(8, 0))
        self.planner_add_opt_btn = _button(opts_head, "+ option", lambda: self._planner_add_option(""), color=PANEL_HI)
        self.planner_add_opt_btn.pack(side="right")
        self.planner_options_frame = tk.Frame(body, bg=PANEL)
        self.planner_options_frame.pack(fill="x")
        self._planner_options: list[tk.StringVar] = []

        row3 = tk.Frame(body, bg=PANEL)
        row3.pack(fill="x", pady=(10, 0))
        tk.Label(row3, text="Voting stays open", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self.planner_duration = tk.Spinbox(
            row3, from_=1, to=scheduled_polls.MAX_DURATION_HOURS, width=5, font=FONT, fg=TEXT, bg=PANEL_HI,
            buttonbackground=PANEL_HI, insertbackground=TEXT, relief="flat",
        )
        self.planner_duration.pack(side="left", padx=(8, 4))
        tk.Label(row3, text="hours", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self._planner_multi = tk.BooleanVar(value=False)
        tk.Checkbutton(
            row3, text="Allow picking more than one", variable=self._planner_multi,
            font=FONT, fg=MUTED, bg=PANEL, selectcolor=PANEL_HI, activebackground=PANEL,
            activeforeground=TEXT, highlightthickness=0,
        ).pack(side="left", padx=(16, 0))

        row4 = tk.Frame(body, bg=PANEL)
        row4.pack(fill="x", pady=(10, 0))
        tk.Label(row4, text="Post on", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self.planner_date = tk.Entry(row4, font=FONT_MONO, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
                                     relief="flat", width=12)
        self.planner_date.pack(side="left", padx=(8, 4), ipady=3)
        tk.Label(row4, text="at", font=FONT, fg=MUTED, bg=PANEL).pack(side="left")
        self.planner_time = tk.Entry(row4, font=FONT_MONO, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
                                     relief="flat", width=9)
        self.planner_time.pack(side="left", padx=(4, 0), ipady=3)
        self.planner_when_note = tk.Label(row4, text="", font=FONT_SM, fg=DIM, bg=PANEL, anchor="w")
        self.planner_when_note.pack(side="left", padx=(10, 0))
        for entry in (self.planner_date, self.planner_time):
            entry.bind("<KeyRelease>", lambda e: self._planner_update_when())

        row5 = tk.Frame(body, bg=PANEL)
        row5.pack(fill="x", pady=(14, 0))
        self.planner_schedule_btn = _button(row5, "Schedule poll", self.schedule_planner_poll, color=PINK_DEEP)
        self.planner_schedule_btn.pack(side="right")
        _button(row5, "Clear", self._planner_reset, color=PANEL_HI).pack(side="right", padx=(0, 6))
        self.planner_status = tk.Label(row5, text="", font=FONT_SM, fg=MUTED, bg=PANEL, anchor="e")
        self.planner_status.pack(side="right", padx=(0, 12), fill="x", expand=True)

        self._build_schedule_list(master).grid(row=0, column=1, sticky="nsew")
        self._planner_reset()

    def _planner_channels_loaded(self, channels: list[tuple[str, str]]):
        self._planner_channel_by_label = {label: cid for cid, label in channels}
        current = self.planner_channel_combo.get()
        self.planner_channel_combo["values"] = [label for _, label in channels]
        if current in self._planner_channel_by_label:
            self.planner_channel_combo.set(current)
        elif channels:
            self.planner_channel_combo.current(0)

    def _planner_add_option(self, text: str):
        if len(self._planner_options) >= scheduled_polls.MAX_OPTIONS:
            return
        self._planner_options.append(tk.StringVar(value=text))
        self._render_planner_options()

    def _planner_remove_option(self, var: tk.StringVar):
        self._planner_options.remove(var)
        self._render_planner_options()

    def _render_planner_options(self):
        for child in self.planner_options_frame.winfo_children():
            child.destroy()
        for i, var in enumerate(self._planner_options, 1):
            row = tk.Frame(self.planner_options_frame, bg=PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{i}.", font=FONT_SM, fg=DIM, bg=PANEL, width=3, anchor="e").pack(side="left")
            tk.Entry(row, textvariable=var, font=FONT, fg=TEXT, bg=PANEL_HI, insertbackground=TEXT,
                     relief="flat").pack(side="left", fill="x", expand=True, padx=(4, 4), ipady=2)
            tk.Button(
                row, text="✕", command=lambda v=var: self._planner_remove_option(v), font=FONT_SM,
                fg=MUTED, bg=PANEL, activebackground=PANEL_HI, activeforeground=RED, relief="flat",
                bd=0, cursor="hand2",
            ).pack(side="left")
        full = len(self._planner_options) >= scheduled_polls.MAX_OPTIONS
        self.planner_add_opt_btn.config(state="disabled" if full else "normal")

    def _planner_fill(self, question: str, options: list[str], duration_hours: int, multi: bool):
        self.planner_question.delete(0, "end")
        self.planner_question.insert(0, question)
        self._planner_options = [tk.StringVar(value=o) for o in options]
        self._render_planner_options()
        self.planner_duration.delete(0, "end")
        self.planner_duration.insert(0, str(duration_hours))
        self._planner_multi.set(multi)

    def _planner_reset(self):
        self.planner_prompt.delete("1.0", "end")
        self._planner_fill("", ["", ""], 24, False)
        # Next full hour at least an hour out — never a default a few minutes away.
        when = (datetime.datetime.now() + datetime.timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        self.planner_date.delete(0, "end")
        self.planner_date.insert(0, when.strftime("%Y-%m-%d"))
        self.planner_time.delete(0, "end")
        self.planner_time.insert(0, when.strftime("%H:%M"))
        self.planner_draft_status.config(text="")
        self.planner_status.config(text="")
        self._planner_update_when()

    @staticmethod
    def _parse_when(date_s: str, time_s: str) -> datetime.datetime:
        """Local date + time → datetime. Takes 2026-10-27 or 10/27(/2026) for
        the date, and 19:30, 7:30pm, or 7pm for the time."""
        date_s, time_s = date_s.strip(), time_s.strip().lower().replace(" ", "")
        today = datetime.date.today()
        date = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                date = datetime.datetime.strptime(date_s, fmt).date()
                break
            except ValueError:
                pass
        if date is None:
            try:
                date = datetime.datetime.strptime(f"{date_s}/{today.year}", "%m/%d/%Y").date()
            except ValueError:
                raise ValueError("can't read that date — try 2026-10-27")
            if date < today:
                date = date.replace(year=today.year + 1)
        for fmt in ("%H:%M", "%I:%M%p", "%I%p"):
            try:
                t = datetime.datetime.strptime(time_s, fmt).time()
                return datetime.datetime.combine(date, t)
            except ValueError:
                pass
        raise ValueError("can't read that time — try 19:30 or 7:30pm")

    def _planner_update_when(self):
        try:
            when = self._parse_when(self.planner_date.get(), self.planner_time.get())
        except ValueError as e:
            self.planner_when_note.config(text=str(e), fg=YELLOW)
            return
        delta = when.timestamp() - time.time()
        label = when.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
        if delta < -60:
            self.planner_when_note.config(text=f"{label} — that's in the past", fg=YELLOW)
            return
        mins = max(0, int(delta // 60))
        span = f"{mins // 1440}d {mins % 1440 // 60}h" if mins >= 1440 else f"{mins // 60}h {mins % 60}m"
        self.planner_when_note.config(text=f"{label}  (in {span}, your local time)", fg=DIM)

    def draft_planner_poll(self):
        label = self.planner_channel_combo.get()
        if not self._planner_channel_by_label.get(label):
            self.planner_draft_status.config(text="pick a channel first", fg=YELLOW)
            return
        prompt = self.planner_prompt.get("1.0", "end").strip()
        if not prompt:
            self.planner_draft_status.config(text="describe the poll you want first", fg=YELLOW)
            return
        self.planner_draft_btn.config(state="disabled")
        self.planner_draft_status.config(text="Claude is drafting…", fg=MUTED)

        def work():
            try:
                poll = scheduled_polls.draft(prompt, label.lstrip("#"))
                self.after(0, lambda: self._planner_drafted(poll, None))
            except Exception as e:
                self.after(0, lambda: self._planner_drafted(None, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _planner_drafted(self, poll: Optional[dict], error: Optional[str]):
        self.planner_draft_btn.config(state="normal")
        if error:
            self.planner_draft_status.config(text=f"failed: {error}", fg=RED)
            return
        self._planner_fill(poll["question"], poll["options"], poll["duration_hours"], poll["allow_multiselect"])
        self.planner_draft_status.config(text="drafted — edit anything below, or redraft", fg=BLUE)

    def schedule_planner_poll(self):
        label = self.planner_channel_combo.get()
        channel_id = self._planner_channel_by_label.get(label)
        if not channel_id:
            self.planner_status.config(text="pick a channel first", fg=YELLOW)
            return
        question = " ".join(self.planner_question.get().split())
        options = [" ".join(v.get().split()) for v in self._planner_options]
        options = [o for o in options if o]
        try:
            duration = int(self.planner_duration.get())
        except ValueError:
            duration = 0
        problem = scheduled_polls.validate(question, options, duration)
        if problem:
            self.planner_status.config(text=problem, fg=YELLOW)
            return
        try:
            when = self._parse_when(self.planner_date.get(), self.planner_time.get())
        except ValueError as e:
            self.planner_status.config(text=str(e), fg=YELLOW)
            return
        if when.timestamp() < time.time() - 60:
            self.planner_status.config(text="that time is in the past", fg=YELLOW)
            return
        scheduled_polls.add(
            channel_id, label.lstrip("#"), self._planner_bot.get(), question, options,
            when.timestamp(), duration, self._planner_multi.get(),
        )
        who = "Porygon Z" if self._planner_bot.get() == "z" else "Porygon"
        activity_log.log(f"\U0001f4ca Poll scheduled for {when:%b %d %H:%M} as {who} ({label}): {question}")
        self._planner_reset()
        self.planner_status.config(text=f"scheduled for {label}", fg=BLUE)
        if not self.pids:
            self.planner_status.config(text=f"scheduled — but the watcher's offline; it posts these", fg=YELLOW)
        self._refresh_schedule(force=True)

    # ---- scheduled list ----
    def _build_schedule_list(self, master):
        outer, body, self.schedule_note = _card(master, "SCHEDULED POLLS")
        wrap = tk.Frame(body, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        self.schedule_canvas = tk.Canvas(wrap, bg=PANEL, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.schedule_canvas.yview, style="Z.Vertical.TScrollbar")
        self.schedule_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.schedule_canvas.pack(side="left", fill="both", expand=True)
        self.schedule_inner = tk.Frame(self.schedule_canvas, bg=PANEL)
        window = self.schedule_canvas.create_window((0, 0), window=self.schedule_inner, anchor="nw")
        self.schedule_inner.bind(
            "<Configure>", lambda e: self.schedule_canvas.configure(scrollregion=self.schedule_canvas.bbox("all")),
        )
        self.schedule_canvas.bind("<Configure>", lambda e: self.schedule_canvas.itemconfigure(window, width=e.width))

        def _wheel(e):
            self.schedule_canvas.yview_scroll(int(-e.delta / 120), "units")
        self.schedule_canvas.bind("<Enter>", lambda e: self.schedule_canvas.bind_all("<MouseWheel>", _wheel))
        self.schedule_canvas.bind("<Leave>", lambda e: self.schedule_canvas.unbind_all("<MouseWheel>"))
        self._schedule_mtime = -1.0
        return outer

    def _poll_schedule(self):
        self._refresh_schedule()
        self.after(3000, self._poll_schedule)

    def _refresh_schedule(self, force: bool = False):
        """Re-render when the file changes — the watcher flips entries to
        posted/failed/missed as it works through them."""
        try:
            mtime = os.path.getmtime(scheduled_polls.SCHEDULE_FILE)
        except OSError:
            mtime = 0.0
        if mtime == self._schedule_mtime and not force:
            return
        self._schedule_mtime = mtime
        entries = scheduled_polls.load()
        pending = sorted((e for e in entries if e.get("status") == "pending"), key=lambda e: e.get("post_at", 0))
        done = sorted((e for e in entries if e.get("status") != "pending"),
                      key=lambda e: e.get("posted_at") or e.get("post_at", 0), reverse=True)
        self.schedule_note.config(text=f"{len(pending)} upcoming", fg=MUTED)
        for child in self.schedule_inner.winfo_children():
            child.destroy()
        if not entries:
            tk.Label(self.schedule_inner, text="Nothing scheduled yet.", font=FONT_SM, fg=DIM, bg=PANEL,
                     anchor="w").pack(fill="x", pady=8)
            return
        for e in pending + done:
            self._schedule_row(e)

    def _schedule_row(self, e: dict):
        chip = {
            "pending": ("UPCOMING", YELLOW), "posted": ("POSTED", BLUE), "failed": ("FAILED", RED),
            "missed": ("MISSED", DIM), "cancelled": ("CANCELLED", DIM),
        }.get(e.get("status"), (str(e.get("status", "?")).upper(), DIM))
        row = tk.Frame(self.schedule_inner, bg=BG, padx=10, pady=7)
        row.pack(fill="x", pady=(0, 6))
        top = tk.Frame(row, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text=chip[0], font=("Bahnschrift SemiBold", 8), fg=BG, bg=chip[1], padx=6).pack(side="left")
        when = datetime.datetime.fromtimestamp(e.get("post_at", 0)).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
        who = "Z" if e.get("bot") == "z" else "Porygon"
        tk.Label(top, text=f"  {when}", font=FONT_B, fg=TEXT, bg=BG).pack(side="left")
        tk.Label(top, text=f"#{e.get('channel_name', '?')} · as {who}", font=FONT_SM, fg=MUTED, bg=BG).pack(side="right")
        tk.Label(row, text=e.get("question", ""), font=FONT, fg=TEXT, bg=BG, anchor="w", justify="left",
                 wraplength=360).pack(fill="x", pady=(4, 0))
        opts = " · ".join(e.get("options", []))
        tk.Label(row, text=opts, font=FONT_SM, fg=MUTED, bg=BG, anchor="w", justify="left",
                 wraplength=360).pack(fill="x")
        if e.get("error"):
            tk.Label(row, text=e["error"], font=FONT_SM, fg=RED, bg=BG, anchor="w").pack(fill="x")
        btns = tk.Frame(row, bg=BG)
        btns.pack(fill="x", pady=(4, 0))
        small = dict(font=FONT_SM, fg=MUTED, bg=BG, activebackground=PANEL_HI, relief="flat", bd=0, cursor="hand2")
        if e.get("status") == "pending":
            tk.Button(btns, text="Cancel", activeforeground=RED, command=lambda: self._schedule_cancel(e),
                      **small).pack(side="right")
            tk.Button(btns, text="Edit", activeforeground=PINK, command=lambda: self._schedule_edit(e),
                      **small).pack(side="right", padx=(0, 8))
        else:
            tk.Button(btns, text="Remove", activeforeground=RED,
                      command=lambda: (scheduled_polls.remove(e["id"]), self._refresh_schedule(force=True)),
                      **small).pack(side="right")
            tk.Button(btns, text="Reuse", activeforeground=PINK, command=lambda: self._schedule_edit(e, keep=True),
                      **small).pack(side="right", padx=(0, 8))

    def _schedule_cancel(self, e: dict):
        if not messagebox.askyesno("Cancel poll?", f"Cancel this scheduled poll?\n\n{e.get('question')}"):
            return
        scheduled_polls.cancel(e["id"])
        self._refresh_schedule(force=True)

    def _schedule_edit(self, e: dict, keep: bool = False):
        """Load a poll back into the editor. Editing an upcoming one takes it
        off the schedule — scheduling again puts the edited version back."""
        if not keep:
            scheduled_polls.remove(e["id"])
            self.planner_status.config(text="taken off the schedule — hit Schedule to put it back", fg=YELLOW)
        label = f"#{e.get('channel_name')}"
        if label in self._planner_channel_by_label:
            self.planner_channel_combo.set(label)
        self._planner_bot.set(e.get("bot", "z"))
        self._planner_fill(e.get("question", ""), e.get("options", []), e.get("duration_hours", 24),
                           e.get("allow_multiselect", False))
        if not keep:
            when = datetime.datetime.fromtimestamp(e.get("post_at", time.time()))
            self.planner_date.delete(0, "end")
            self.planner_date.insert(0, when.strftime("%Y-%m-%d"))
            self.planner_time.delete(0, "end")
            self.planner_time.insert(0, when.strftime("%H:%M"))
        self._planner_update_when()
        self._refresh_schedule(force=True)


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
            self.after(0, lambda: self._set_pids(pids))
        threading.Thread(target=work, daemon=True).start()

    def _set_pids(self, pids: list[int]):
        if set(pids) != set(self.pids):
            self._pids_since = time.time()
        self.pids = pids

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
        elif self.pids and time.time() - self._pids_since > WATCHER_STUCK_SECONDS:
            pill, color = "STUCK", RED
            mins = int((time.time() - self.status.get("heartbeat", self._pids_since)) // 60)
            doing = (
                f"Watcher is running but hasn't scanned in {mins}m — if its console window "
                "says “Select”, click it and press Esc; otherwise Stop + Start."
            )
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
