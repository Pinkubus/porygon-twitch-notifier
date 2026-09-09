"""
quotes.py — poll-based "quote bot" feature, integrated into loop.py's 30s cycle.

Two behaviors, scanned across every text channel in the guild:
  1. A message starting with "!addquote <text>" saves <text> as a quote and
     reacts with 📝 to confirm.
  2. Any other new message whose content contains a saved quote (case-
     insensitive substring) gets a ™️ reaction — a lightweight "catbot"-style
     callback.

Only ever looks at messages newer than the last-seen message per channel
(quotes_scan_state.json), so it never re-scans/re-reacts to old history and
stays cheap (one "list channels" + one "list messages after X" call per
channel per cycle).
"""
from __future__ import annotations

import os
import json
import logging
import time

import activity_log
import discord_roles

logger = logging.getLogger("porygon.quotes")

QUOTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quotes.json")
SCAN_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quotes_scan_state.json")

ADD_PREFIX = "!addquote "
SAVED_REACTION = "📝"
CALLBACK_REACTION = "™"  # Discord rejects the fully-qualified "™️" (with VS16) as "Unknown Emoji"


def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
        return default


def _save_json(path: str, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_quotes() -> list[dict]:
    return _load_json(QUOTES_FILE, [])


def save_quotes(quotes: list[dict]):
    _save_json(QUOTES_FILE, quotes)


def load_scan_state() -> dict[str, str]:
    return _load_json(SCAN_STATE_FILE, {})


def save_scan_state(state: dict[str, str]):
    _save_json(SCAN_STATE_FILE, state)


def is_configured() -> bool:
    return bool(os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_GUILD_ID"))


def scan_and_process(bot_user_id: str) -> bool:
    """Returns True if quotes.json or quotes_scan_state.json changed."""
    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = os.environ["DISCORD_GUILD_ID"]

    quotes = load_quotes()
    quote_texts_lower = [q["text"].lower() for q in quotes]
    scan_state = load_scan_state()

    changed_quotes = False
    changed_scan = False

    channels = discord_roles.get_guild_text_channels(guild_id, token)
    for channel in channels:
        channel_id = channel["id"]
        after = scan_state.get(channel_id)

        if after is None:
            # First time seeing this channel — seed the cursor without
            # reacting to/scanning years of backlog.
            latest = discord_roles.get_channel_messages(channel_id, token, limit=1)
            if latest:
                scan_state[channel_id] = latest[0]["id"]
                changed_scan = True
            continue

        messages = discord_roles.get_channel_messages(channel_id, token, after=after, limit=100)
        if not messages:
            continue

        max_id = after
        # Discord returns newest-first; process oldest-first for intuitive ordering.
        for msg in sorted(messages, key=lambda m: int(m["id"])):
            msg_id = msg["id"]
            if int(msg_id) > int(max_id):
                max_id = msg_id

            author_id = msg.get("author", {}).get("id")
            if author_id == bot_user_id:
                continue

            content = (msg.get("content") or "").strip()
            if not content:
                continue

            if content.lower().startswith(ADD_PREFIX):
                text = content[len(ADD_PREFIX):].strip()
                if text and text.lower() not in quote_texts_lower:
                    quotes.append({
                        "text": text,
                        "added_by": author_id,
                        "channel_id": channel_id,
                        "message_id": msg_id,
                        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    })
                    quote_texts_lower.append(text.lower())
                    changed_quotes = True
                    if discord_roles.add_own_reaction(channel_id, msg_id, SAVED_REACTION, token):
                        logger.info(f"Saved quote: \"{text[:80]}\"")
                        activity_log.log(f"\u2705 Quote saved: \"{text[:80]}\"")
                    else:
                        logger.warning(f"Quote saved but confirm reaction failed: \"{text[:80]}\"")
                        activity_log.log(f"\u26a0\ufe0f Quote saved but confirm reaction failed: \"{text[:80]}\"")
                continue

            lowered = content.lower()
            if any(qt and qt in lowered for qt in quote_texts_lower):
                if discord_roles.add_own_reaction(channel_id, msg_id, CALLBACK_REACTION, token):
                    logger.info(f"Quote callback reaction added ({channel_id}/{msg_id})")
                    activity_log.log("\u2705 Quote callback reaction added")
                else:
                    logger.warning(f"Quote callback reaction failed ({channel_id}/{msg_id})")
                    activity_log.log("\u274c Quote callback reaction failed")

        scan_state[channel_id] = max_id
        changed_scan = True

    if changed_quotes:
        save_quotes(quotes)
    if changed_scan:
        save_scan_state(scan_state)
    return changed_quotes or changed_scan
