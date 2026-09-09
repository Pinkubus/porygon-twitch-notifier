"""
activity_log.py — append-only plain-text log of "signal" events (Twitch
live notifications, quote saves/callback reactions, reaction-role
grants/revokes — NOT scan/plumbing calls) committed to the repo alongside
state.json/reaction_state.json/quotes.json.

This exists because GitHub Actions job logs for a still-running job can't
be downloaded (`gh run view --log` errors with "logs will be available
when it is complete") — there's no public API for tailing an in-progress
job's raw stdout. Committing each event here lets a local watcher
(porygon_logger.bat / watch_activity.py) see successes/failures in near
real time just by `git pull`-ing this file.
"""
from __future__ import annotations

import os
import time

_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity.log")
_dirty = False


def log(message: str):
    """Append a timestamped line to activity.log and mark it dirty."""
    global _dirty
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}Z] {message}\n")
    _dirty = True


def flush_if_dirty() -> bool:
    """True (and clears the flag) if `log()` was called since the last flush."""
    global _dirty
    if _dirty:
        _dirty = False
        return True
    return False
