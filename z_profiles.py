"""
z_profiles.py — builds Porygon Z's read on each server member.

Runs on a slow cadence (default every 12 hours) and hands each person's
recent messages to z_memory, which keeps a short summary plus a growing list
of tagged memories, so Z can make callbacks instead of generic jokes. Profiles are written to
z_user_profiles.json, which is gitignored — characterizations of real people
stay on the machine that generated them rather than going up to a public repo.
"""
from __future__ import annotations

import os
import time
import logging

import discord_roles
import z_brain

logger = logging.getLogger("porygon.z_profiles")

_SCAN_INTERVAL = float(os.environ.get("Z_PROFILE_SCAN_HOURS", 12)) * 3600
_MIN_MESSAGES = int(os.environ.get("Z_PROFILE_MIN_MESSAGES", 15))
# Vent channels are read only for people who opted in (comma-separated user
# ids; default just father). Everyone else's venting is never memory material.
_VENT_OPT_IN = {
    u.strip() for u in os.environ.get("Z_VENT_MEMORY_USERS", z_brain.FATHER_USER_ID).split(",") if u.strip()
}


_MAX_PAGES = int(os.environ.get("Z_PROFILE_MAX_PAGES", 10))
_FIRST_PAGES = int(os.environ.get("Z_PROFILE_FIRST_PAGES", 3))
_LEARN_CHUNK = 150
_LEARN_MAX_CHUNKS = int(os.environ.get("Z_PROFILE_LEARN_CHUNKS", 3))


def _new_messages(channel_id: str, token: str, cursor) -> list[dict]:
    """Everything newer than `cursor` (up to _MAX_PAGES pages of 100), or
    the last _FIRST_PAGES pages for a channel never scanned before."""
    out, before = [], None
    for _ in range(_MAX_PAGES if cursor else _FIRST_PAGES):
        page = discord_roles.get_channel_messages(channel_id, token, before=before, limit=100) or []
        if not page:
            break
        if cursor:
            fresh = [m for m in page if int(m["id"]) > int(cursor)]
            out += fresh
            if len(fresh) < len(page):
                break
        else:
            out += page
        before = min(page, key=lambda m: int(m["id"]))["id"]
        if len(page) < 100:
            break
    return out


def due(profiles: dict) -> bool:
    return time.time() - profiles.get("_last_scan", 0) >= _SCAN_INTERVAL


def scan(bot_user_ids: set[str], force: bool = False) -> bool:
    """Refresh every member's summary and fold new tagged memories into
    z_memory. Returns True if anything changed. `force` ignores the cadence
    (the panel's Learn now button)."""
    import z_memory  # deferred: z_memory imports z_brain
    import z_self

    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = os.environ["DISCORD_GUILD_ID"]

    profiles = z_brain.load_profiles()
    if not force and not due(profiles):
        return False

    # Each channel remembers where the last scan stopped, so a scan reads
    # everything new since then instead of just the newest 100 messages,
    # which a busy channel scrolls past in hours.
    cursors = profiles.setdefault("_cursors", {})
    by_user: dict[str, dict] = {}
    self_lines: list[str] = []
    # The first self-memory scan reads back a few pages per channel, since
    # the people cursors have already moved past everything said so far.
    self_backfill = z_self.backfill_needed()
    for channel in discord_roles.get_guild_text_channels(guild_id, token):
        # Vent channels: only opted-in people's messages, and z_memory's prompt
        # keeps just light everyday quirks from them, flagged gentle.
        vent = z_brain.is_delicate(channel.get("name", ""))
        cid = channel["id"]
        msgs = _new_messages(cid, token, cursors.get(cid))
        if not vent:
            seen = {m["id"] for m in msgs}
            older = [m for m in _new_messages(cid, token, None) if m["id"] not in seen] if self_backfill else []
            self_lines += z_self.relevant_lines(older + msgs, bot_user_ids)
        if msgs:
            cursors[cid] = max((m["id"] for m in msgs), key=int)
        for msg in sorted(msgs, key=lambda m: int(m["id"])):
            author = msg.get("author", {})
            uid = author.get("id")
            content = (msg.get("content") or "").strip()
            if not uid or uid in bot_user_ids or author.get("bot") or not content:
                continue
            if vent and uid not in _VENT_OPT_IN:
                continue
            entry = by_user.setdefault(
                uid, {"name": author.get("global_name") or author.get("username", uid), "messages": []},
            )
            entry["messages"].append((content, vent))

    changed = False
    for uid, entry in by_user.items():
        if len(entry["messages"]) < _MIN_MESSAGES:
            continue
        msgs = entry["messages"][-_LEARN_CHUNK * _LEARN_MAX_CHUNKS:]
        for i in range(0, len(msgs), _LEARN_CHUNK):
            try:
                changed |= z_memory.learn(uid, entry["name"], msgs[i:i + _LEARN_CHUNK])
            except Exception as e:
                logger.warning(f"Memory update failed for {entry['name']}: {e}")

    for i in range(0, min(len(self_lines), 750), 250):
        try:
            changed |= z_self.learn(self_lines[i:i + 250])
        except Exception as e:
            logger.warning(f"Self-memory update failed: {e}")
    if self_backfill:
        z_self.mark_backfilled()

    latest = z_brain.load_profiles()
    latest["_cursors"] = cursors
    profiles = latest
    profiles["_last_scan"] = time.time()
    z_brain.save_profiles(profiles)
    logger.info(f"Profiled {sum(1 for k in profiles if not k.startswith('_'))} users")
    return changed
