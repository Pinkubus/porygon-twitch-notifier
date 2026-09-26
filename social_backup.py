"""
social_backup.py — `!post <platform>`: a manual backup for when
social_watch.py's poller hasn't (yet) picked up someone's new post.

Deletes the triggering message and posts the requesting user's latest post
on the named platform into SOCIAL_POST_CHANNEL_ID, exactly as an automatic
announcement would. The platform name is fuzzy-matched (aliases, typos,
case — `!post ig`, `!post Insta`, `!post twiiter` all resolve) against
whatever the user is actually registered for. If it can't tell which one
they mean — no argument given, or genuinely ambiguous — it asks via a
native Discord poll instead of guessing, and only that person's vote
resolves it (checked each watch cycle via Discord's poll-voters endpoint,
since there's no gateway connection here to be told about a vote directly).

Every successful `!post` is logged as a "the auto-poller missed this"
signal — needing a manual backup at all means social_watch should have
caught it on its own, which is worth surfacing even when the fetch/post
itself succeeds.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import time
from typing import Optional

import activity_log
import discord_roles
import social_profiles
import social_watch

logger = logging.getLogger("porygon.social_backup")

COMMAND = "!post"
PENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social_post_pending.json")

# An unanswered disambiguation poll is dropped after this long — same spirit
# as social_profiles'/z_polls' pending TTLs.
PENDING_TTL_SECONDS = int(os.environ.get("SOCIAL_POST_POLL_TTL", 3600))
POLL_DURATION_HOURS = 1


def is_command(content: str) -> bool:
    stripped = content.strip().lower()
    return stripped == COMMAND or stripped.startswith(COMMAND + " ")


def _load_pending() -> dict:
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load {PENDING_FILE}: {e}")
        return {}


def _save_pending(pending: dict) -> None:
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2, ensure_ascii=False)


def _match_platform(arg: str, candidates: list[str]) -> tuple[Optional[str], list[str]]:
    """Fuzzy-match `arg` against `candidates` (the platforms this user is
    actually registered for). Returns (confident match or None, plausible
    candidates for a poll if not confident)."""
    arg = (arg or "").strip().lower()
    if not arg:
        return None, candidates

    exact = social_profiles.resolve_alias(arg)
    if exact and exact in candidates:
        return exact, [exact]

    pool: dict[str, str] = {}  # alias word -> platform
    for platform in candidates:
        pool[platform] = platform
        for alias in social_profiles.PLATFORM_ALIASES.get(platform, ()):
            pool[alias] = platform

    # 0.6 (difflib's usual default) is loose enough that "twitch" and
    # "twitter" typos bleed into each other (they share a long common
    # prefix) and trigger a poll for what should be an obvious match; 0.75
    # still catches realistic typos (tested) while telling those two apart.
    close = difflib.get_close_matches(arg, pool.keys(), n=5, cutoff=0.75)
    matched = []
    for word in close:
        platform = pool[word]
        if platform not in matched:
            matched.append(platform)

    if len(matched) == 1:
        return matched[0], matched
    return None, matched or candidates


def _ask_which_platform(
    user_id: str, display_name: str, channel_id: str, trigger_message_id: str,
    candidates: list[str], token: str,
) -> None:
    labels = [social_watch.PLATFORM_LABEL.get(p, p.capitalize()) for p in candidates]
    result = discord_roles.post_poll_with_answer_ids(
        channel_id, token, f"{display_name}, which platform did you mean for !post?",
        labels, duration_hours=POLL_DURATION_HOURS,
    )
    # The poll now carries the context, so the trigger message goes either way.
    discord_roles.delete_message(channel_id, trigger_message_id, token)

    if result is None:
        msg = f"Failed to post !post disambiguation poll for {display_name}"
        logger.warning(msg)
        activity_log.log(f"❌ {msg}")
        return

    poll_message_id, answer_ids = result
    answers = {
        str(answer_ids[label]): platform
        for label, platform in zip(labels, candidates) if label in answer_ids
    }
    pending = _load_pending()
    pending[poll_message_id] = {
        "user_id": user_id, "display_name": display_name, "channel_id": channel_id,
        "answers": answers, "created_at": time.time(),
    }
    _save_pending(pending)


def _post_backup(
    user_id: str, display_name: str, platform: str, channel_id: str,
    trigger_message_id: Optional[str], token: str,
) -> Optional[tuple[str, str]]:
    """Returns (announcement_channel_id, announcement_message_id) on a
    successful post — callers that need to reference or clean up the
    announcement (e.g. a one-off test) can use it; normal callers ignore it."""
    label = social_watch.PLATFORM_LABEL.get(platform, platform.capitalize())

    reason = social_watch.unsupported_reason(platform)
    if reason:
        if trigger_message_id:
            discord_roles.post_reply(channel_id, trigger_message_id, token, f"Can't !post {label} yet — {reason}")
            discord_roles.delete_message(channel_id, trigger_message_id, token)
        msg = f"!post backup rejected: {display_name} tried {label}, which isn't supported yet ({reason})"
        logger.info(msg)
        activity_log.log(f"⚠️ {msg}")
        return None

    profile = social_profiles.load_profiles().get(user_id) or {}
    handles = (profile.get("platforms") or {}).get(platform) or []

    if not handles:
        if trigger_message_id:
            discord_roles.post_reply(channel_id, trigger_message_id, token, f"You're not registered for {label} anymore.")
            discord_roles.delete_message(channel_id, trigger_message_id, token)
        msg = f"!post backup failed: {display_name} is no longer registered for {label}"
        logger.warning(msg)
        activity_log.log(f"⚠️ {msg}")
        return None

    # Registering multiple handles under one platform is supported, but
    # `!post` can only mean one of them — the first one, consistently.
    handle = handles[0]
    post = social_watch.fetch_latest(platform, handle)

    if post is None:
        if trigger_message_id:
            discord_roles.post_reply(
                channel_id, trigger_message_id, token,
                f"Couldn't fetch anything from {label} for @{handle} right now — try again in a bit.",
            )
            discord_roles.delete_message(channel_id, trigger_message_id, token)
        msg = f"!post backup failed: couldn't fetch {label} for {display_name} (@{handle})"
        logger.warning(msg)
        activity_log.log(f"❌ {msg}")
        return None

    if trigger_message_id:
        discord_roles.delete_message(channel_id, trigger_message_id, token)

    # Whether social_watch's own cursor already matched this post is a
    # useful diagnostic even when !post itself works: it tells you whether
    # this was a genuine miss or just someone not trusting/noticing the
    # auto-post.
    already_seen = social_watch.get_cursor(platform, handle) == post["id"]
    announced_message_id = social_watch.announce(user_id, display_name, platform, handle, post, token)
    social_watch.record_seen(platform, handle, post["id"])

    if not announced_message_id:
        msg = f"!post backup: fetched a {label} post for {display_name} but failed to announce it"
        logger.warning(msg)
        activity_log.log(f"❌ {msg}")
        return None

    logger.info(f"!post backup posted for {display_name} ({label})")
    if already_seen:
        activity_log.log(f"ℹ️ {display_name} ran !post for {label} — that post was already auto-announced")
    else:
        activity_log.log(
            f"\U0001f4cc {display_name} used !post as a backup for {label} — "
            "the auto-watcher hadn't picked this up on its own yet"
        )
    return social_watch.POST_CHANNEL_ID, announced_message_id


def handle_command(msg: dict, channel_id: str, token: str) -> None:
    """Handle one `!post` message. Never raises — quotes.py's sweep must
    keep going."""
    author = msg.get("author", {}) or {}
    user_id = author.get("id")
    if not user_id:
        return
    display_name = author.get("global_name") or author.get("username") or user_id
    arg = (msg.get("content") or "").strip()[len(COMMAND):].strip()

    profile = social_profiles.load_profiles().get(user_id)
    registered = list((profile or {}).get("platforms", {}) or {})

    if not registered:
        discord_roles.post_reply(
            channel_id, msg["id"], token,
            "You're not registered for anything yet — try `!register` first.",
        )
        discord_roles.delete_message(channel_id, msg["id"], token)
        msg_text = f"{display_name} ran !post but isn't registered for anything"
        logger.info(msg_text)
        activity_log.log(f"⚠️ {msg_text}")
        return

    if len(registered) == 1:
        _post_backup(user_id, display_name, registered[0], channel_id, msg["id"], token)
        return

    target, candidates = _match_platform(arg, registered)
    if target is None:
        _ask_which_platform(user_id, display_name, channel_id, msg["id"], candidates, token)
        return
    _post_backup(user_id, display_name, target, channel_id, msg["id"], token)


def prune_and_resolve_pending() -> None:
    """Drop expired disambiguation polls, and resolve any the asking user
    has voted on since the last check. Call once per watch cycle."""
    pending = _load_pending()
    if not pending:
        return
    token = os.environ["DISCORD_BOT_TOKEN"]
    now = time.time()
    changed = False

    for poll_message_id, entry in list(pending.items()):
        if now - entry.get("created_at", 0) > PENDING_TTL_SECONDS:
            del pending[poll_message_id]
            changed = True
            continue

        resolved_platform = None
        for answer_id, platform in entry.get("answers", {}).items():
            voters = discord_roles.get_poll_answer_voters(entry["channel_id"], poll_message_id, int(answer_id), token)
            if entry.get("user_id") in voters:
                resolved_platform = platform
                break

        if resolved_platform:
            _post_backup(
                entry["user_id"], entry["display_name"], resolved_platform,
                entry["channel_id"], None, token,
            )
            del pending[poll_message_id]
            changed = True

    if changed:
        _save_pending(pending)
