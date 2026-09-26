"""
social_profiles.py — the `!register` command: anyone can tell Porygon which
social accounts to watch, in plain English, and social_watch.py posts here
when those accounts put something new up.

`!register` (no arguments) replies with a short form explaining the format.
Anyone replies to *that* message (a real Discord reply) listing accounts, one
platform per line — `twitter: @a @b`, `instagram: handle`, etc. — and
registers *themselves*, whether or not they're the one who ran the command;
the form is a shared sign-up sheet, not a private one-time link, so it stays
usable for whoever gets to it next. No comma/format requirement: everything
that looks like a handle or profile URL on the line is picked up. Replying
`clear` removes the replier's own registration instead.

Power users can skip the form entirely: `!register twitter: @a` in one shot.

Scanned by quotes.py, on its own cursor (`_scan_social_commands`) rather than
the shared one !feature/!addquote use — see that module's docstring for why:
the shared cursor is also advanced by a cloud pass that never has social
features on, and sharing it let a real `!register` get marked "seen" by a
pass that couldn't act on it, never to be looked at again.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import activity_log
import discord_roles

logger = logging.getLogger("porygon.social_profiles")

PROFILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social_profiles.json")
PENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social_pending.json")

COMMAND = "!register"
CONFIRM_REACTION = "\U0001f4cb"  # 📋
SAVED_REACTION = "✅"  # ✅
CLEARED_REACTION = "\U0001f5d1️"  # 🗑️

CLEAR_WORDS = {"clear", "none", "remove", "unregister", "stop"}

# Canonical platform -> recognized leading words on a line (checked
# case-insensitively). Order doesn't matter; longest-match isn't needed since
# these are compared whole-word.
PLATFORM_ALIASES: dict[str, tuple[str, ...]] = {
    "twitter": ("twitter", "x"),
    "instagram": ("instagram", "ig", "insta"),
    "youtube": ("youtube", "yt"),
    "tiktok": ("tiktok", "tt"),
    "twitch": ("twitch",),
}
_ALIAS_TO_PLATFORM = {
    alias.lower(): platform
    for platform, aliases in PLATFORM_ALIASES.items()
    for alias in ((aliases,) if isinstance(aliases, str) else aliases)
}

# Platforms social_watch.py actually knows how to poll today — everything
# else is stored (so registering it now costs nothing once support lands)
# but flagged in the confirmation as not-yet-monitored. TikTok is the one
# left out on purpose: its post list requires a signed request from a real
# browser session and just returns empty otherwise (tested).
_ACTIVELY_MONITORED = {"youtube", "instagram", "twitter", "twitch"}

_LINE_RE = re.compile(r"^\s*([A-Za-z]+)\s*[:\-–—]?\s*(.*)$")
_URL_RE = re.compile(r"https?://\S+")
_TOKEN_RE = re.compile(r"@?([A-Za-z0-9._-]{2,50})")
_STOPWORDS = {"and", "or", "the", "a", "n", "none", "skip", "na", "n/a"}

# An unanswered form is dropped after this long, matching z_polls' pending
# clarification TTL so a form nobody filled out can't be replied to weeks
# later against a conversation that moved on.
PENDING_TTL_SECONDS = int(os.environ.get("SOCIAL_REGISTER_TTL", 24 * 3600))

FORM_TEXT = (
    "**Social tracker sign-up** — reply to *this* message (an actual Discord "
    "reply) listing the accounts you want me watching, one platform per "
    "line:\n"
    "```\n"
    "twitter: @yourhandle\n"
    "instagram: yourhandle another_handle\n"
    "youtube: @yourchannel\n"
    "```\n"
    "Any separator works, multiple handles per line are fine, and skip "
    "whatever platforms you don't use — a profile URL works too. Reply "
    "`clear` instead to stop being tracked.\n"
    "-# Actively watched today: YouTube, Instagram, Twitch, Twitter/X (if a "
    "Nitter mirror is configured). TikTok is stored but not polled yet."
)

def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
        return default


def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_profiles() -> dict:
    return _load_json(PROFILES_FILE, {})


def _save_profiles(profiles: dict):
    _save_json(PROFILES_FILE, profiles)


def _load_pending() -> dict:
    return _load_json(PENDING_FILE, {})


def _save_pending(pending: dict):
    _save_json(PENDING_FILE, pending)


def is_command(content: str) -> bool:
    stripped = content.strip().lower()
    return stripped == COMMAND or stripped.startswith(COMMAND + " ")


def resolve_alias(word: str) -> Optional[str]:
    """'ig'/'x'/'yt'/'tt' (or a canonical name) -> canonical platform, or
    None. Shared with social_backup.py's `!post` fuzzy matching."""
    return _ALIAS_TO_PLATFORM.get(word.strip().lower())


def prune_pending() -> None:
    """Drop forms nobody replied to in time."""
    pending = _load_pending()
    now = time.time()
    stale = [k for k, v in pending.items() if now - v.get("created_at", 0) > PENDING_TTL_SECONDS]
    if not stale:
        return
    for key in stale:
        del pending[key]
    _save_pending(pending)


def _extract_handles(blob: str) -> list[str]:
    handles: list[str] = []
    remainder = blob
    for url in _URL_RE.findall(blob):
        remainder = remainder.replace(url, " ")
        path = url.split("?", 1)[0].rstrip("/")
        seg = path.rsplit("/", 1)[-1].lstrip("@")
        if seg:
            handles.append(seg)
    for tok in _TOKEN_RE.findall(remainder):
        tok = tok.strip("._-")
        if tok and tok.lower() not in _STOPWORDS:
            handles.append(tok)
    seen: set[str] = set()
    out: list[str] = []
    for h in handles:
        key = h.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def parse_registration(text: str) -> tuple[dict[str, list[str]], list[str]]:
    """Free-text reply -> ({platform: [handles]}, [unrecognized lines]).

    Deliberately lenient: no required separator, no required comma-joining of
    multiple handles, aliases (ig/yt/x/tt) accepted.
    """
    platforms: dict[str, list[str]] = {}
    unrecognized: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("*-• ")
        if not line:
            continue
        match = _LINE_RE.match(line)
        if not match:
            unrecognized.append(raw_line.strip())
            continue
        word, rest = match.group(1).lower(), match.group(2)
        platform = _ALIAS_TO_PLATFORM.get(word)
        if platform is None:
            unrecognized.append(raw_line.strip())
            continue
        handles = _extract_handles(rest)
        if not handles:
            unrecognized.append(raw_line.strip())
            continue
        platforms.setdefault(platform, [])
        for h in handles:
            if h.lower() not in (x.lower() for x in platforms[platform]):
                platforms[platform].append(h)
    return platforms, unrecognized


def _summary_lines(platforms: dict[str, list[str]]) -> list[str]:
    lines = []
    for platform, handles in platforms.items():
        tag = "" if platform in _ACTIVELY_MONITORED else " (not monitored yet)"
        lines.append(f"• {platform.capitalize()}{tag}: " + ", ".join(f"@{h}" for h in handles))
    return lines


def _post_form(msg: dict, channel_id: str, token: str) -> None:
    existing = load_profiles().get(msg.get("author", {}).get("id") or "", {})
    text = FORM_TEXT
    if existing.get("platforms"):
        text = "You're currently tracked as:\n" + "\n".join(_summary_lines(existing["platforms"])) + "\n\n" + text
    reply_id = discord_roles.post_reply(channel_id, msg["id"], token, text)
    if reply_id is None:
        logger.warning(f"Failed to post registration form ({channel_id}/{msg['id']})")
        return
    pending = _load_pending()
    pending[reply_id] = {
        "user_id": msg.get("author", {}).get("id"),
        "channel_id": channel_id,
        "created_at": time.time(),
    }
    _save_pending(pending)
    discord_roles.add_own_reaction(channel_id, msg["id"], CONFIRM_REACTION, token)


def _apply_registration(user_id: str, author: dict, text: str, channel_id: str, reply_to_id: str, token: str) -> None:
    profiles = load_profiles()

    if text.strip().lower() in CLEAR_WORDS:
        if user_id in profiles:
            del profiles[user_id]
            _save_profiles(profiles)
            activity_log.log(f"\U0001f5d1️ Social tracking cleared for {author.get('username', user_id)}")
        discord_roles.post_reply(channel_id, reply_to_id, token, "Cleared — you're no longer tracked.")
        discord_roles.add_own_reaction(channel_id, reply_to_id, CLEARED_REACTION, token)
        return

    platforms, unrecognized = parse_registration(text)
    if not platforms:
        discord_roles.post_reply(
            channel_id, reply_to_id, token,
            "Couldn't find any recognizable platform/handle in that — try e.g. "
            "`twitter: @yourhandle`, one per line. Reply to the form again to retry.",
        )
        return

    name = author.get("global_name") or author.get("username") or user_id
    profiles[user_id] = {
        "name": name,
        "platforms": platforms,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_profiles(profiles)

    lines = ["✅ Registered! I'll watch:"] + _summary_lines(platforms)
    if unrecognized:
        lines.append("⚠️ Couldn't parse: " + "; ".join(f"\"{u}\"" for u in unrecognized[:5]))
    discord_roles.post_reply(channel_id, reply_to_id, token, "\n".join(lines))
    discord_roles.add_own_reaction(channel_id, reply_to_id, SAVED_REACTION, token)
    logger.info(f"Registered {name} ({user_id}): {list(platforms.keys())}")
    activity_log.log(f"\U0001f4cb {name} registered for social tracking: {', '.join(platforms.keys())}")


def handle_command(msg: dict, channel_id: str, token: str) -> None:
    """Handle one `!register` message. Never raises — quotes.py's sweep must
    keep going."""
    content = (msg.get("content") or "").strip()
    inline = content[len(COMMAND):].strip()
    author = msg.get("author", {}) or {}
    user_id = author.get("id")
    if not user_id:
        return

    if inline:
        # Power-user path: everything after the command is the registration
        # itself, no form round-trip needed.
        _apply_registration(user_id, author, inline, channel_id, msg["id"], token)
        return

    _post_form(msg, channel_id, token)


def maybe_handle_reply(msg: dict, channel_id: str, token: str) -> bool:
    """If `msg` is a reply to any pending registration form, register
    whoever replied and return True. Not just the person who originally ran
    `!register` — a form sitting in a public channel is fair game for anyone
    to fill out for *themselves*, so the reply's own author is who gets
    registered, using their own content, regardless of whose command
    produced the form. (This used to require the replier to match the form's
    original requester; that made a form silently useless to everyone but
    whoever happened to run the command, which is exactly the opposite of
    a shared sign-up sheet.)

    The pending record is left in place afterward rather than consumed, so
    the same form keeps working for the next person too — it's only ever
    dropped by prune_pending() once nobody has used it in TTL."""
    ref = msg.get("message_reference") or {}
    ref_id = ref.get("message_id")
    if not ref_id:
        return False

    pending = _load_pending()
    if ref_id not in pending:
        return False

    author = msg.get("author", {}) or {}
    replier_id = author.get("id")
    if not replier_id:
        return False

    content = (msg.get("content") or "").strip()
    if not content:
        return False

    _apply_registration(replier_id, author, content, channel_id, msg["id"], token)
    return True
