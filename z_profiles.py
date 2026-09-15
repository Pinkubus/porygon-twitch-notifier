"""
z_profiles.py — builds Porygon Z's read on each server member.

Runs on a slow cadence (default daily) and samples each person's recent
messages into a couple of sentences about how they talk, so Z can make
callbacks instead of generic jokes. Profiles are written to
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

_SCAN_INTERVAL = int(os.environ.get("Z_PROFILE_SCAN_HOURS", 24)) * 3600
_MIN_MESSAGES = int(os.environ.get("Z_PROFILE_MIN_MESSAGES", 15))
_SAMPLE_PER_CHANNEL = 100


def due(profiles: dict) -> bool:
    return time.time() - profiles.get("_last_scan", 0) >= _SCAN_INTERVAL


def scan(bot_user_ids: set[str]) -> bool:
    """Refresh every member's profile. Returns True if anything changed."""
    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = os.environ["DISCORD_GUILD_ID"]

    profiles = z_brain.load_profiles()
    if not due(profiles):
        return False

    by_user: dict[str, dict] = {}
    for channel in discord_roles.get_guild_text_channels(guild_id, token):
        if z_brain.is_delicate(channel.get("name", "")):
            continue  # venting channels aren't material for a personality read
        for msg in discord_roles.get_channel_messages(
            channel["id"], token, limit=_SAMPLE_PER_CHANNEL,
        ):
            author = msg.get("author", {})
            uid = author.get("id")
            content = (msg.get("content") or "").strip()
            if not uid or uid in bot_user_ids or author.get("bot") or not content:
                continue
            entry = by_user.setdefault(
                uid, {"name": author.get("global_name") or author.get("username", uid), "messages": []},
            )
            entry["messages"].append(content)

    changed = False
    for uid, entry in by_user.items():
        if len(entry["messages"]) < _MIN_MESSAGES:
            continue
        summary = z_brain.summarize_user(entry["name"], entry["messages"])
        if summary:
            profiles[uid] = {"name": entry["name"], "summary": summary, "updated": time.time()}
            changed = True

    profiles["_last_scan"] = time.time()
    z_brain.save_profiles(profiles)
    logger.info(f"Profiled {sum(1 for k in profiles if not k.startswith('_'))} users")
    return changed
