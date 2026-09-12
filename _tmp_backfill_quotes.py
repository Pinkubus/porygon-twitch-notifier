"""
One-off backfill: re-scan channel history since the recent Twitch-token
outage for quote callback matches that never got reacted to (either because
the loop was down, or because the matching quote was added after the
message was posted — the normal cursor-based scan never revisits old
messages). Also verifies the custom `porygonwow` emoji resolves correctly.

Run via workflow_dispatch, then delete both this file and its workflow.
"""
import os
import sys
import time
import logging

import discord_roles
import quotes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill")

# Comfortably before the last known-good run (2026-09-09T05:09:21Z), so we
# don't miss anything right at the boundary.
CUTOFF_ISO = "2026-09-09T04:30:00Z"
DISCORD_EPOCH = 1420070400000


def snowflake_from_iso(iso: str) -> str:
    import calendar
    t = time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    ms = calendar.timegm(t) * 1000 - DISCORD_EPOCH
    return str(ms << 22)


def reacted_already(msg: dict, callback_reaction: str) -> bool:
    reactions = msg.get("reactions") or []
    if ":" in callback_reaction:
        _, emoji_id = callback_reaction.split(":", 1)
        return any(r.get("me") and r.get("emoji", {}).get("id") == emoji_id for r in reactions)
    return any(r.get("me") and r.get("emoji", {}).get("name") == callback_reaction for r in reactions)


def main():
    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = os.environ["DISCORD_GUILD_ID"]

    callback_reaction = quotes.get_callback_reaction(guild_id, token)
    logger.info(f"Resolved callback reaction: {callback_reaction}")
    if ":" not in callback_reaction:
        logger.warning("Custom 'porygonwow' emoji NOT found — check the emoji name/spelling in the server.")

    saved_quotes = quotes.load_quotes()
    quote_texts_lower = [q["text"].lower() for q in saved_quotes]
    bot_user_id = discord_roles.get_bot_user_id(token)

    cutoff = snowflake_from_iso(CUTOFF_ISO)
    channels = discord_roles.get_guild_text_channels(guild_id, token)

    total_scanned = 0
    total_matched = 0
    total_added = 0
    total_already = 0
    total_failed = 0

    for channel in channels:
        channel_id = channel["id"]
        after = cutoff
        while True:
            messages = discord_roles.get_channel_messages(channel_id, token, after=after, limit=100)
            if not messages:
                break
            messages = sorted(messages, key=lambda m: int(m["id"]))
            for msg in messages:
                after = msg["id"]
                total_scanned += 1
                if msg.get("author", {}).get("id") == bot_user_id:
                    continue
                content = (msg.get("content") or "").strip()
                if not content:
                    continue
                lowered = content.lower()
                if not any(qt and qt in lowered for qt in quote_texts_lower):
                    continue
                total_matched += 1
                if reacted_already(msg, callback_reaction):
                    total_already += 1
                    continue
                if discord_roles.add_own_reaction(channel_id, msg["id"], callback_reaction, token):
                    total_added += 1
                    logger.info(f"Backfilled reaction on {channel_id}/{msg['id']}: \"{content[:60]}\"")
                else:
                    total_failed += 1
                    logger.warning(f"Failed to react on {channel_id}/{msg['id']}")
            if len(messages) < 100:
                break

    logger.info(
        f"Done. scanned={total_scanned} matched={total_matched} "
        f"already_reacted={total_already} newly_added={total_added} failed={total_failed}"
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
