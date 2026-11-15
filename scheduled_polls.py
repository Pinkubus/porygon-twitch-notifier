"""
scheduled_polls.py — polls planned ahead of time in the control panel's
Poll Planner tab, posted by the watcher once their time comes.

The panel drafts a poll with Claude (draft()), lets father edit it, and
saves it here with a post time (add()). The watcher calls post_due() every
cycle (~10s) and posts whatever has come due. A poll whose time passed while
the watcher was offline still goes up when it comes back, unless it's more
than LATE_LIMIT_SECONDS late — a "what are we playing tonight" poll landing
two days later is worse than none, so those are marked missed instead.

scheduled_polls.json is local-only and gitignored, the same as the other
panel <-> watcher files (see z_live.py). Every write re-reads the file first
and changes only the one entry it's about, so the panel adding a poll and the
watcher posting another in the same instant can't clobber each other.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Optional

import activity_log
import discord_roles
import z_brain
import z_polls

logger = logging.getLogger("porygon.scheduled_polls")

_HERE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = os.path.join(_HERE, "scheduled_polls.json")

LATE_LIMIT_SECONDS = int(os.environ.get("SCHEDULED_POLL_LATE_LIMIT", 6 * 3600))
DRAFT_MODEL = os.environ.get("SCHEDULED_POLL_MODEL", z_brain.MODEL_COMPOSE)

MAX_OPTIONS = z_polls.DISCORD_MAX_ANSWERS
MAX_QUESTION_CHARS = z_polls.DISCORD_MAX_QUESTION_CHARS
MAX_OPTION_CHARS = z_polls.DISCORD_MAX_ANSWER_CHARS
MAX_DURATION_HOURS = z_polls.DISCORD_MAX_DURATION_HOURS

_lock = threading.Lock()


# ---- storage ----------------------------------------------------------------

def load() -> list[dict]:
    try:
        with open(SCHEDULE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError, OSError):
        return []


def _save(entries: list[dict]) -> None:
    tmp = f"{SCHEDULE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    for _ in range(5):
        try:
            os.replace(tmp, SCHEDULE_FILE)
            return
        except PermissionError:
            time.sleep(0.05)


def _update(entry_id: str, **fields) -> None:
    with _lock:
        entries = load()
        for e in entries:
            if e.get("id") == entry_id:
                e.update(fields)
        _save(entries)


def add(
    channel_id: str, channel_name: str, bot: str, question: str, options: list[str],
    post_at: float, duration_hours: int, allow_multiselect: bool,
) -> dict:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "channel_id": channel_id,
        "channel_name": channel_name,
        "bot": bot,
        "question": question,
        "options": options,
        "duration_hours": duration_hours,
        "allow_multiselect": allow_multiselect,
        "post_at": post_at,
        "created_at": time.time(),
        "status": "pending",
    }
    with _lock:
        entries = load()
        entries.append(entry)
        _save(entries)
    return entry


def cancel(entry_id: str) -> None:
    _update(entry_id, status="cancelled")


def remove(entry_id: str) -> None:
    with _lock:
        _save([e for e in load() if e.get("id") != entry_id])


def validate(question: str, options: list[str], duration_hours: int) -> Optional[str]:
    """Discord's own poll limits, checked up front so a bad poll is caught in
    the panel instead of 400ing at post time with nobody watching."""
    if not question:
        return "the poll needs a question"
    if len(question) > MAX_QUESTION_CHARS:
        return f"question is {len(question)} chars (Discord's max is {MAX_QUESTION_CHARS})"
    if len(options) < 2:
        return "a poll needs at least 2 options"
    if len(options) > MAX_OPTIONS:
        return f"{len(options)} options (Discord's max is {MAX_OPTIONS})"
    long = [o for o in options if len(o) > MAX_OPTION_CHARS]
    if long:
        return f"option “{long[0][:20]}…” is over {MAX_OPTION_CHARS} chars"
    if len({o.casefold() for o in options}) != len(options):
        return "two options are the same"
    if not 1 <= duration_hours <= MAX_DURATION_HOURS:
        return f"duration must be 1–{MAX_DURATION_HOURS} hours"
    return None


# ---- drafting ---------------------------------------------------------------

_DRAFT_SYSTEM = f"""\
You write Discord polls for a small, close-knit friend server. You get a
description of the poll someone wants and the channel it will go in. Write
one poll that does what they asked.

- The question is short, clear, and plainly worded — at most
  {MAX_QUESTION_CHARS} characters, ideally under 100.
- Options are distinct, parallel in form, and each at most
  {MAX_OPTION_CHARS} characters. Between 2 and {MAX_OPTIONS} of them; if the
  request names a number, use it. Otherwise pick whatever number the
  question naturally has, usually 3 to 6.
- If they listed specific options, use exactly those, tidied up but not
  replaced.
- Set allow_multiselect true only when picking several clearly makes sense
  ("which nights work for you"), otherwise false.
- duration_hours is how long voting stays open: default 24, longer if the
  request implies it ("this week" means 168). Max {MAX_DURATION_HOURS}.

Return ONLY a JSON object, no prose:
{{"question": "...", "options": ["...", "..."], "allow_multiselect": false, "duration_hours": 24}}
"""


def draft(prompt: str, channel_name: str) -> dict:
    """Ask Claude for a poll. Returns {question, options, allow_multiselect,
    duration_hours}, already clamped to Discord's limits; raises
    RuntimeError with a message fit to show in the panel."""
    user = f"Channel: #{channel_name}\n\nThe poll they want:\n{prompt}"
    raw = z_brain._call(DRAFT_MODEL, _DRAFT_SYSTEM, user, max_tokens=4000, purpose="draft_scheduled_poll")
    if raw is None:
        raise RuntimeError("Claude call failed — check the log")
    data = z_polls._json_object(raw)
    if not data:
        raise RuntimeError("Claude's reply wasn't a readable poll — try again")
    question = z_polls._fit(data.get("question") or "", MAX_QUESTION_CHARS)
    options = [z_polls._fit(o, MAX_OPTION_CHARS) for o in data.get("options") or [] if str(o).strip()]
    try:
        duration = int(data.get("duration_hours") or 24)
    except (TypeError, ValueError):
        duration = 24
    return {
        "question": question,
        "options": options[:MAX_OPTIONS],
        "allow_multiselect": bool(data.get("allow_multiselect")),
        "duration_hours": max(1, min(MAX_DURATION_HOURS, duration)),
    }


# ---- posting (watcher side) -------------------------------------------------

def _token_for(bot: str) -> str:
    if bot == "z":
        import porygon_z  # deferred: porygon_z is heavy and imports half the project
        return porygon_z._z_token()
    return os.environ["DISCORD_BOT_TOKEN"]


def post_due(now: Optional[float] = None) -> bool:
    """Post every pending poll whose time has come. Returns True if anything
    changed, so the caller knows activity.log has something new."""
    now = now if now is not None else time.time()
    changed = False
    for entry in load():
        if entry.get("status") != "pending" or entry.get("post_at", 0) > now:
            continue
        changed = True
        late = now - entry["post_at"]
        where = f"#{entry.get('channel_name', entry.get('channel_id'))}"
        if late > LATE_LIMIT_SECONDS:
            _update(entry["id"], status="missed")
            activity_log.log(
                f"\U0001f4ca Scheduled poll skipped — watcher was offline, {late / 3600:.0f}h past its time "
                f"({where}): {entry.get('question')}"
            )
            continue
        message_id = discord_roles.post_poll(
            entry["channel_id"], _token_for(entry.get("bot", "z")), entry["question"], entry["options"],
            duration_hours=entry.get("duration_hours", 24),
            allow_multiselect=entry.get("allow_multiselect", False),
        )
        if message_id:
            _update(entry["id"], status="posted", message_id=message_id, posted_at=now)
            activity_log.log(f"\U0001f4ca Scheduled poll posted ({where}): {entry['question']}")
        else:
            _update(entry["id"], status="failed", error="Discord rejected it — see the watcher log")
            activity_log.log(f"\U0001f4ca Scheduled poll FAILED to post ({where}): {entry['question']}")
    return changed
