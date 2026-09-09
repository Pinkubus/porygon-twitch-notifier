"""
find_common_phrases.py — one-off scan across recent channel history to
surface things people say a lot (inside jokes), as quote candidates for a
human to review and `!addquote` manually. Doesn't touch quotes.json itself.

Looks at exact repeated messages (normalized) AND repeated 4-8 word phrases
within longer messages, each requiring a minimum number of occurrences from
a minimum number of distinct authors (to filter out one person spamming).

Env vars: DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, and optionally
HISTORY_LIMIT (messages per channel, default 500), MIN_OCCURRENCES
(default 3), MIN_AUTHORS (default 2).
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict

import discord_roles

_WORD_RE = re.compile(r"\S+")


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _fetch_recent_messages(channel_id: str, token: str, limit: int) -> list[dict]:
    messages: list[dict] = []
    before = None
    while len(messages) < limit:
        page = discord_roles.get_channel_messages(channel_id, token, before=before, limit=min(100, limit - len(messages)))
        if not page:
            break
        messages.extend(page)
        before = page[-1]["id"]
        if len(page) < 100:
            break
    return messages


def main() -> int:
    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = os.environ["DISCORD_GUILD_ID"]
    guild_display_id = os.environ.get("DISCORD_GUILD_ID", guild_id)
    history_limit = int(os.environ.get("HISTORY_LIMIT") or 500)
    min_occurrences = int(os.environ.get("MIN_OCCURRENCES") or 3)
    min_authors = int(os.environ.get("MIN_AUTHORS") or 2)
    bot_user_id = discord_roles.get_bot_user_id(token)

    # normalized text -> {"count": int, "authors": set, "channel_id":, "message_id":}
    exact: dict[str, dict] = {}
    ngrams: dict[str, dict] = {}

    channels = discord_roles.get_guild_text_channels(guild_id, token)
    for channel in channels:
        channel_id = channel["id"]
        messages = _fetch_recent_messages(channel_id, token, history_limit)
        print(f"Scanned {len(messages)} messages in #{channel.get('name')}")

        for msg in messages:
            author_id = msg.get("author", {}).get("id")
            if author_id == bot_user_id:
                continue
            content = (msg.get("content") or "").strip()
            if not content or content.lower().startswith("!"):
                continue

            norm = _normalize(content)
            if 4 <= len(norm) <= 200:
                entry = exact.setdefault(norm, {"count": 0, "authors": set(), "channel_id": channel_id, "message_id": msg["id"]})
                entry["count"] += 1
                entry["authors"].add(author_id)

            words = _WORD_RE.findall(norm)
            seen_in_message = set()
            for n in range(4, 9):
                for i in range(len(words) - n + 1):
                    gram = " ".join(words[i:i + n])
                    if gram in seen_in_message:
                        continue
                    seen_in_message.add(gram)
                    entry = ngrams.setdefault(gram, {"count": 0, "authors": set(), "channel_id": channel_id, "message_id": msg["id"]})
                    entry["count"] += 1
                    entry["authors"].add(author_id)

    def report(title: str, data: dict[str, dict], covered: set[str]):
        candidates = [
            (text, e) for text, e in data.items()
            if e["count"] >= min_occurrences and len(e["authors"]) >= min_authors and text not in covered
        ]
        candidates.sort(key=lambda kv: kv[1]["count"], reverse=True)
        print(f"\n=== {title} ({len(candidates)} candidates) ===")
        for text, e in candidates[:30]:
            link = f"https://discord.com/channels/{guild_display_id}/{e['channel_id']}/{e['message_id']}"
            print(f"[{e['count']}x, {len(e['authors'])} authors] {text!r} — {link}")
        return {text for text, _ in candidates}

    covered = report("Exact repeated messages", exact, set())
    report("Repeated phrases within messages", ngrams, covered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
