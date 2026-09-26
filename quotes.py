"""
quotes.py — poll-based "quote bot" feature, integrated into loop.py's 30s cycle.

Two behaviors, scanned across every text channel in the guild:
  1. A message starting with "!addquote <text>" saves <text> as a quote and
     reacts with 📝 to confirm.
  2. Any other new message whose content contains a saved quote (case-
     insensitive substring) gets a custom `porygonwow` reaction — a
     lightweight "catbot"-style callback.

This sweep is also where `!feature` is picked up (feature_requests.py) —
piggy-backing on it keeps that command free of extra Discord API calls.

Only ever looks at messages newer than the last-seen message per channel
(quotes_scan_state.json), so it never re-scans/re-reacts to old history and
stays cheap (one "list channels" + one "list messages after X" call per
channel per cycle).

`quotes_scan_state.json` is committed and shared: both the GitHub Actions
5-minute pass and a local `quotes_watch_local.py` advance it, and either one's
push can fast-forward the other's copy past messages it never actually acted
on for anything that pass doesn't do. That's harmless for quotes/`!feature` —
the cloud pass handles those itself, so its own advance is real progress. It
is NOT harmless for `!register`/`!post`: those only ever run where
`social_watch.is_configured()` is true, which today means the local watcher
only (Instagram/etc. need a residential IP, so social is deliberately kept
off the cloud job's env). A social command that lands in the same batch as a
cloud pass gets its cursor advanced by a process that never looked at it —
and the next local pass, seeing `after` already past that message, never
gets a second look either. (This is exactly what happened to the first real
`!register` attempt, on 2026-09-22 — seen by the cloud pass, never answered.)

So social commands are scanned on their own cursor (`social_scan_state.json`,
gitignored — never committed, so nothing else can ever fast-forward it) via
`_scan_social_commands`, entirely decoupled from the shared one above. It
costs one extra "list messages" call per channel, and only when
`social_enabled` — i.e., only on whichever process actually has social
switched on. That's still ~30 calls (measured: ~15s) across a real server's
channels, so it only runs every `Z_SOCIAL_SCAN_EVERY_N_SCANS`-th cycle
(default 6) rather than every one: `!register`/`!post` were never
latency-sensitive the way a porygonwow reaction is, and piling those ~30
calls onto the main loop's own ~30 *every* cycle was slow enough to blow
past porygon_z's own 60s offline-detection threshold on every pass — a real,
hours-long regression this shipped with before being caught. A wall-clock
throttle ("don't run again within 60s") doesn't actually help once a cycle
is already slower than that window — more than 60s elapses between calls
either way — so this counts calls instead, which caps the cost regardless
of how long any individual cycle takes.
"""
from __future__ import annotations

import os
import json
import logging
import time
from typing import Optional

import activity_log
import discord_roles
import feature_requests
import social_backup
import social_profiles
import social_watch

logger = logging.getLogger("porygon.quotes")

QUOTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quotes.json")
SCAN_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quotes_scan_state.json")
# Local-only (gitignored) on purpose — see the module docstring. Never touched
# by anything but the process that actually has social features switched on,
# so nothing else can ever advance it past a message social hasn't seen.
SOCIAL_SCAN_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social_scan_state.json")

ADD_PREFIX = "!addquote "
SAVED_REACTION = "📝"
CALLBACK_EMOJI_NAME = "porygonwow"  # custom server emoji
# Keyed by guild_id, not a single value — a custom emoji only reacts cleanly
# in the guild it belongs to, and a second server won't have this one, so
# each guild resolves (and falls back) independently.
_callback_reaction_cache: dict[str, str] = {}
_scan_count = 0

# The main loop's cadence (loop.py/quotes_watch_local.py both call
# scan_and_process every ~10-30s) is tuned for quote/porygonwow reactions
# feeling instant. `!register`/`!post` were never that latency-sensitive —
# nobody needs sub-minute registration — but _scan_social_commands originally
# ran on that same every-cycle cadence anyway: one extra "list messages" call
# per configured channel (~30 on this server), on top of the ~30 the main
# loop's own per-channel loop already makes. Measured directly against this
# server, each of those ~30-call passes costs ~15s on its own (Discord's own
# per-route pacing, not a bug) — so this alone was adding ~15s to every
# cycle, which was enough to push a cycle over porygon_z's 60s heartbeat-gap
# threshold. That falsely looked like downtime, which triggered a jugglez
# backfill scan every cycle too (a heavier pass), which then kept the cycle
# slow enough to keep tripping the same "was it down?" check — a genuinely
# hours-long regression before anyone noticed the log spam.
#
# A wall-clock throttle doesn't actually help here: once a cycle already
# takes longer than the throttle window, "don't run again for 60s" is true
# on every single call anyway, since more than 60s always elapses between
# calls. What actually cuts the average cost is a hard count-based skip —
# run once every _SOCIAL_SCAN_EVERY_N_SCANS calls, full stop, regardless of
# how long any individual cycle takes.
_SOCIAL_SCAN_EVERY_N_SCANS = int(os.environ.get("Z_SOCIAL_SCAN_EVERY_N_SCANS", 6))


def get_callback_reaction(guild_id: str, token: str) -> str:
    """Resolve the custom `porygonwow` emoji to its `name:id` reaction form
    for this guild, cached per process. Falls back to a bare ™ if it's ever
    missing (e.g. a second server that doesn't have the emoji)."""
    if guild_id not in _callback_reaction_cache:
        emojis = discord_roles.get_guild_emojis(guild_id, token)
        match = next((e for e in emojis if e.get("name") == CALLBACK_EMOJI_NAME), None)
        if match:
            _callback_reaction_cache[guild_id] = f"{CALLBACK_EMOJI_NAME}:{match['id']}"
        else:
            logger.warning(f"Custom emoji '{CALLBACK_EMOJI_NAME}' not found in guild {guild_id} — falling back to ™")
            _callback_reaction_cache[guild_id] = "™"
    return _callback_reaction_cache[guild_id]


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


def _load_social_scan_state() -> dict[str, str]:
    return _load_json(SOCIAL_SCAN_STATE_FILE, {})


def _save_social_scan_state(state: dict[str, str]):
    _save_json(SOCIAL_SCAN_STATE_FILE, state)


def is_configured() -> bool:
    return bool(os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_GUILD_ID"))


def _scan_social_commands(token: str, bot_user_id: str) -> bool:
    """`!register`/`!post`, on a cursor nothing but this function ever writes.

    Mirrors the main loop's per-channel fetch-and-advance shape (including
    the same no-backlog-on-first-sight seeding), but keeps its own state file
    so a pass that doesn't have social features on can never carry this
    cursor forward on social's behalf — see the module docstring."""
    scan_state = _load_social_scan_state()
    changed = False

    for guild_id in discord_roles.configured_guild_ids():
        for channel in discord_roles.get_guild_text_channels(guild_id, token):
            channel_id = channel["id"]
            after = scan_state.get(channel_id)

            if after is None:
                latest = discord_roles.get_channel_messages(channel_id, token, limit=1)
                if latest:
                    scan_state[channel_id] = latest[0]["id"]
                    changed = True
                continue

            messages = discord_roles.get_channel_messages(channel_id, token, after=after, limit=100)
            if not messages:
                continue

            max_id = after
            for msg in sorted(messages, key=lambda m: int(m["id"])):
                msg_id = msg["id"]
                if int(msg_id) > int(max_id):
                    max_id = msg_id

                if msg.get("author", {}).get("id") == bot_user_id:
                    continue
                content = (msg.get("content") or "").strip()
                if not content:
                    continue

                if social_profiles.is_command(content):
                    logger.info(f"Social scan: !register from {msg.get('author', {}).get('username')} ({channel_id}/{msg_id})")
                    social_profiles.handle_command(msg, channel_id, token)
                elif social_backup.is_command(content):
                    logger.info(f"Social scan: !post from {msg.get('author', {}).get('username')} ({channel_id}/{msg_id})")
                    social_backup.handle_command(msg, channel_id, token)
                else:
                    social_profiles.maybe_handle_reply(msg, channel_id, token)

            scan_state[channel_id] = max_id
            changed = True

    social_profiles.prune_pending()
    social_backup.prune_and_resolve_pending()

    if changed:
        _save_social_scan_state(scan_state)
    return changed


def scan_and_process(bot_user_id: str) -> bool:
    """Returns True if quotes.json or quotes_scan_state.json changed."""
    global _scan_count
    token = os.environ["DISCORD_BOT_TOKEN"]

    quotes = load_quotes()
    quote_texts_lower = [q["text"].lower() for q in quotes]
    scan_state = load_scan_state()

    changed_quotes = False
    changed_scan = False
    messages_seen = 0
    total_channels = 0

    social_enabled = social_watch.is_configured()

    for guild_id in discord_roles.configured_guild_ids():
        callback_reaction = get_callback_reaction(guild_id, token)
        channels = discord_roles.get_guild_text_channels(guild_id, token)
        total_channels += len(channels)
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
            messages_seen += len(messages)

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

                if feature_requests.is_command(content):
                    feature_requests.handle(msg, channel_id, token)
                    continue

                # !register/!post are NOT handled here — see _scan_social_commands
                # and the module docstring for why they need their own cursor.

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
                    if discord_roles.add_own_reaction(channel_id, msg_id, callback_reaction, token):
                        logger.info(f"Quote callback reaction added ({channel_id}/{msg_id})")
                        activity_log.log("\u2705 Quote callback reaction added")
                    else:
                        logger.warning(f"Quote callback reaction failed ({channel_id}/{msg_id})")
                        activity_log.log("\u274c Quote callback reaction failed")

            scan_state[channel_id] = max_id
            changed_scan = True

    # Cheap no-op unless a request is sitting on a fallback title (e.g. one
    # the API-key-less Actions pass recorded).
    feature_requests.upgrade_pending_titles()
    changed_social = False
    if social_enabled and _scan_count % _SOCIAL_SCAN_EVERY_N_SCANS == 0:
        changed_social = _scan_social_commands(token, bot_user_id)

    if changed_quotes:
        save_quotes(quotes)
    if changed_scan:
        save_scan_state(scan_state)
    _scan_count += 1
    logger.info(f"Quotes scan #{_scan_count}: {messages_seen} message(s) across {total_channels} channel(s)")
    return changed_quotes or changed_scan or changed_social
