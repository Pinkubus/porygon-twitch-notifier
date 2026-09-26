"""
z_live.py — local, gitignored side channel between the running watcher
(quotes_watch_local.py -> porygon_z.py) and the control panel
(porygon_panel.py).

Two small JSON files, neither ever committed:

  z_live_status.json   written by the watcher: heartbeat, what Z is
                       drafting right now (and for whom), and when it last
                       replied to each user.
  z_controls.json      written by the panel: live overrides for Z's knobs
                       (pause, per-feature toggles, thresholds, odds). The
                       watcher re-reads it at the start of every scan, so
                       changes land within one poll cycle — no restart.

Everything here is best-effort: a failed status write must never break a
scan, and a missing/corrupt controls file just means "use .env defaults".
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(_HERE, "z_live_status.json")
CONTROLS_FILE = os.path.join(_HERE, "z_controls.json")

_lock = threading.Lock()


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _write_json(path: str, data: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # On Windows the replace can lose a race with a reader holding the file
    # open for a split second — just try again.
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)


def _update_status(mutate) -> None:
    with _lock:
        try:
            status = _read_json(STATUS_FILE)
            mutate(status)
            _write_json(STATUS_FILE, status)
        except Exception:
            pass


# --- watcher side -----------------------------------------------------------

def seconds_since_last_heartbeat() -> Optional[float]:
    """Gap since the previous process's last heartbeat, read BEFORE this
    cycle's heartbeat() call overwrites it — None on a first-ever run (no
    prior status file). A large gap means genuine downtime (crash, hang, PC
    off), not just a slow cycle, and callers use it to catch up on anything
    that only gets checked within a recent window rather than off a cursor."""
    status = _read_json(STATUS_FILE)
    last = status.get("heartbeat")
    return (time.time() - last) if last else None


def heartbeat(reply_count: int, switches: dict, settings: dict) -> None:
    """Once per scan: proves the watcher is alive, and publishes the
    effective switches/knob values (plus .env defaults) for the panel."""
    def _m(s):
        s["heartbeat"] = time.time()
        s["pid"] = os.getpid()
        s["reply_count"] = reply_count
        s["switches"] = switches
        s["settings"] = settings
    _update_status(_m)


def start_processing(user: dict, channel_name: str, kind: str) -> float:
    """Z has started drafting a reply to `user` (a Discord author dict).
    Returns the `since` timestamp this draft was filed under — callers hold
    onto it and pass it to consume_cancel() right before actually posting,
    so a cancel request can be tied to this exact draft rather than just
    "whoever's row is busy right now"."""
    since = time.time()
    def _m(s):
        s["processing"] = {
            "user_id": user.get("id"),
            "name": user.get("global_name") or user.get("username") or user.get("id"),
            "channel": channel_name,
            "kind": kind,
            "since": since,
            "scores": [],
        }
    _update_status(_m)
    return since


def progress(score: Optional[float]) -> None:
    if score is None:
        return
    def _m(s):
        if s.get("processing"):
            s["processing"].setdefault("scores", []).append(score)
    _update_status(_m)


def set_processing_mode(mode: str) -> None:
    def _m(s):
        if s.get("processing"):
            s["processing"]["mode"] = mode
    _update_status(_m)


def finish_processing() -> None:
    def _m(s):
        s["processing"] = None
    _update_status(_m)


def set_api_call(model: str, purpose: str) -> None:
    """Marks one Anthropic call as in flight — the single most useful thing
    to have on disk if the watcher ever freezes again: whichever call was
    last marked and never cleared is the one that hung, and `since` says for
    how long. Cleared in a finally, so a normal (even a failed) call always
    clears it; only a genuine hang leaves it stuck."""
    def _m(s):
        s["api_call"] = {"model": model, "purpose": purpose, "since": time.time()}
    _update_status(_m)


def clear_api_call() -> None:
    def _m(s):
        s["api_call"] = None
    _update_status(_m)


def record_reply(user: dict, channel_name: str, kind: str, text: str) -> None:
    def _m(s):
        uid = user.get("id")
        if not uid:
            return
        s.setdefault("last_reply", {})[uid] = {
            "ts": time.time(),
            "name": user.get("global_name") or user.get("username") or uid,
            "channel": channel_name,
            "kind": kind,
            "text": text,
        }
    _update_status(_m)


def load_controls() -> dict:
    return _read_json(CONTROLS_FILE)


# --- cancelling an in-flight draft ------------------------------------------
# For the panel's "stop this one, I'll answer it myself" button. A cancel
# request is tied to the exact draft (user id + its `since` timestamp), not
# just "whoever's busy" — a click that lands right as that draft finishes
# must not reach forward and cancel some unrelated later one for the same
# person. It rides on CONTROLS_FILE (panel writes, watcher reads/consumes)
# rather than a new file, since it's the same "panel tells the watcher
# something" channel everything else here already uses — just a one-shot
# command instead of a standing setting, consumed (deleted) the moment
# whichever draft it names checks for it, match or not, so a stale request
# can never linger and misfire on a later draft that happens to reuse it.


def request_cancel(user_id: str) -> bool:
    """Panel side. True if there was actually a live draft for `user_id` to
    flag for cancellation; False if the row wasn't busy by the time this ran
    (nothing to do — the button shouldn't normally be clickable then)."""
    with _lock:
        status = _read_json(STATUS_FILE)
        proc = status.get("processing")
        if not proc or proc.get("user_id") != user_id or proc.get("since") is None:
            return False
        controls = _read_json(CONTROLS_FILE)
        controls["cancel_processing"] = {"user_id": user_id, "since": proc["since"]}
        _write_json(CONTROLS_FILE, controls)
        return True


def consume_cancel(user_id: str, since: float) -> bool:
    """Watcher side, checked right before posting a reply it drafted for
    `user_id` starting at `since`. True only if father asked to cancel
    exactly this draft. Removes the request either way it resolves, once
    checked, so a non-matching or already-answered one can't linger."""
    with _lock:
        controls = _read_json(CONTROLS_FILE)
        req = controls.get("cancel_processing")
        if not req:
            return False
        matched = req.get("user_id") == user_id and req.get("since") == since
        if matched:
            del controls["cancel_processing"]
            _write_json(CONTROLS_FILE, controls)
        return matched


# --- panel side -------------------------------------------------------------

def load_status() -> dict:
    return _read_json(STATUS_FILE)


def save_controls(controls: dict) -> None:
    _write_json(CONTROLS_FILE, controls)
