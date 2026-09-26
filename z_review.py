"""z_review.py — sample real server messages, show what Porygon Z would reply
with, and collect a hand-typed verdict on each — then hand the whole batch to
Claude Code to tune Z's persona from.

Pulls N (default 10) random real messages from random channels and random
points in each channel's history (not just recent ones), drafts Z's reply to
each with the same "always answers" pipeline a direct summon uses, and asks
for your verdict one at a time. Type "pass", "fail", a reason for either, or
just write the line the way you'd rather it read — anything goes, it's all
just recorded as free text and handed to Claude Code to interpret.

Read-only against Discord: never posts, reacts, or touches any state file, so
it's safe to re-run as often as you like.

Usage:
    python z_review.py               # 10 random messages
    python z_review.py --count 20    # a bigger batch
    python z_review.py --no-handoff  # just print/save the transcript, skip Claude Code
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import subprocess
import sys
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))

# Drafting is Claude/Discord API-bound, not CPU-bound, so a handful of
# messages can draft concurrently without stepping on each other.
MAX_PARALLEL_DRAFTS = int(os.environ.get("Z_REVIEW_PARALLEL", 5))


def _load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(_HERE, ".env"))

import discord_roles  # noqa: E402
import porygon_z  # noqa: E402
import z_brain  # noqa: E402

# Discord snowflakes encode a millisecond timestamp in their high bits.
_DISCORD_EPOCH_MS = 1420070400000


def _snowflake_to_ts(snowflake: str) -> float:
    return ((int(snowflake) >> 22) + _DISCORD_EPOCH_MS) / 1000


def _channel_bounds(channel_id: str, token: str) -> Optional[tuple[int, int]]:
    """(oldest_id, newest_id) for a channel, or None if it has no messages."""
    newest = discord_roles.get_channel_messages(channel_id, token, limit=1)
    if not newest:
        return None
    oldest = discord_roles.get_channel_messages(channel_id, token, after="0", limit=1)
    if not oldest:
        return None
    return int(oldest[0]["id"]), int(newest[0]["id"])


def _is_usable(msg: dict) -> bool:
    if msg.get("author", {}).get("bot"):
        return False
    return bool((msg.get("content") or "").strip())


def sample_messages(guild_id: str, token: str, count: int) -> list[dict]:
    """Pick `count` distinct real messages from random channels and random
    points in each channel's full history."""
    channels = [c for c in discord_roles.get_guild_text_channels(guild_id, token)]
    bounds_cache: dict[str, Optional[tuple[int, int]]] = {}
    picked: list[dict] = []
    seen_ids: set[str] = set()

    attempts = 0
    max_attempts = count * 30
    while len(picked) < count and attempts < max_attempts and channels:
        attempts += 1
        channel = random.choice(channels)
        channel_id = channel["id"]
        channel_name = channel.get("name", "")
        print(f"  [{len(picked)}/{count}] checking #{channel_name} (attempt {attempts}/{max_attempts})...", flush=True)

        if channel_id not in bounds_cache:
            bounds_cache[channel_id] = _channel_bounds(channel_id, token)
        bounds = bounds_cache[channel_id]
        if bounds is None:
            continue
        oldest_id, newest_id = bounds
        if oldest_id >= newest_id:
            candidates = discord_roles.get_channel_messages(channel_id, token, limit=1)
        else:
            around = str(random.randint(oldest_id, newest_id))
            candidates = discord_roles.get_channel_messages(channel_id, token, around=around, limit=5)

        random.shuffle(candidates)
        for msg in candidates:
            if msg["id"] in seen_ids or not _is_usable(msg):
                continue
            msg["_channel_id"] = channel_id
            msg["_channel_name"] = channel_name
            picked.append(msg)
            seen_ids.add(msg["id"])
            print(f"  [{len(picked)}/{count}] picked a message from #{channel_name}", flush=True)
            break

    return picked


def draft_reply(
    msg: dict, token: str, reply_count: int, recent_replies: list[str], label: str = "",
) -> Optional[str]:
    prefix = f"{label} " if label else "  "
    print(f"{prefix}building context...", flush=True)
    context, user_ids = z_brain.build_context(msg["_channel_id"], msg, token)

    def _heartbeat(attempt: int, max_attempts: Optional[int]) -> None:
        print(f"{prefix}drafting attempt {attempt}...", flush=True)

    return z_brain.compose_reply(
        context, msg, msg["_channel_name"], reply_count, user_ids, recent_replies=recent_replies,
        progress_cb=_heartbeat,
    )


def _author_name(msg: dict) -> str:
    author = msg.get("author", {})
    return author.get("global_name") or author.get("username") or "someone"


def run_review(count: int) -> list[dict]:
    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = os.environ["DISCORD_GUILD_ID"]

    try:
        with open(os.path.join(_HERE, "porygon_z_state.json")) as f:
            z_state = json.load(f)
    except Exception:
        z_state = {}
    reply_count = z_state.get("_reply_count", 0)
    recent_replies = porygon_z._load_recent_replies(porygon_z.RECENT_REPLIES_LIMIT)

    print(f"Sampling {count} random messages across the server...\n", flush=True)
    messages = sample_messages(guild_id, token, count)
    if not messages:
        print("Couldn't find any usable messages (no human messages in any channel?).")
        return []
    print(f"\nGot {len(messages)} messages. Drafting Z's replies in parallel (up to "
          f"{MAX_PARALLEL_DRAFTS} at once, uncapped attempts each) "
          "so the review itself is rapid-fire...\n", flush=True)

    def _draft_one(i: int, msg: dict) -> tuple[int, dict, str, str, Optional[str]]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(_snowflake_to_ts(msg["id"])))
        content = (msg.get("content") or "").strip()
        label = f"[{i}/{len(messages)} #{msg['_channel_name']}]"
        print(f"{label} queued — {when} — {_author_name(msg)}: {content}", flush=True)
        # Each thread gets its own snapshot of recent_replies — they run
        # concurrently so they can't meaningfully chain off one another.
        reply = draft_reply(msg, token, reply_count, list(recent_replies), label=label)
        print(f"{label} done.", flush=True)
        return i, msg, when, content, reply

    drafted_by_index: dict[int, tuple[dict, str, str, Optional[str]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_DRAFTS) as pool:
        futures = [pool.submit(_draft_one, i, msg) for i, msg in enumerate(messages, 1)]
        for future in concurrent.futures.as_completed(futures):
            i, msg, when, content, reply = future.result()
            drafted_by_index[i] = (msg, when, content, reply)

    drafted = [drafted_by_index[i] for i in range(1, len(messages) + 1)]

    print(f"\nAll {len(drafted)} drafted. Answer each in turn:\n", flush=True)

    results = []
    for i, (msg, when, content, reply) in enumerate(drafted, 1):
        print(f"--- {i}/{len(drafted)} — #{msg['_channel_name']} — {when} ---")
        print(f"{_author_name(msg)}: {content}")
        if reply:
            print(f"Z would say: {reply}")
            print(f"  (as posted: {z_brain.glitchify(reply)})")
        else:
            print("Z would say: (nothing — held its own bar)")
        answer = input("Your answer (pass / fail / edit / reason): ").strip()
        print()

        results.append({
            "channel": msg["_channel_name"],
            "when": when,
            "author": _author_name(msg),
            "message": content,
            "reply": reply,
            "answer": answer,
        })

    return results


def build_handoff_prompt(results: list[dict]) -> str:
    lines = [
        "Review results from a random sample of real server messages and what "
        "Porygon Z drafted in reply to each. For each item, my \"answer\" is "
        "free text: \"pass\" means the line was good as-is, \"fail\" means it "
        "missed, and anything else is either a reason for that verdict or my "
        "own rewrite of the line Z should have said — use judgement to tell "
        "which. Tweak Porygon Z's persona/prompt/rubric in this repo "
        "according to these answers, then restart the local watcher "
        "(quotes_watch_local.py) once you've applied the changes so they "
        "take effect.\n",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}) #{r['channel']} ({r['when']})")
        lines.append(f"   {r['author']}: {r['message']}")
        lines.append(f"   Z said: {r['reply'] or '(nothing — held)'}")
        lines.append(f"   My answer: {r['answer'] or '(blank)'}")
        lines.append("")
    return "\n".join(lines)


def hand_off_to_claude_code(prompt: str) -> bool:
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt], cwd=_HERE, timeout=1800,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        print("Claude Code timed out.")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10, help="how many messages to sample")
    ap.add_argument("--no-handoff", action="store_true", help="skip invoking Claude Code")
    args = ap.parse_args()

    if not os.environ.get("DISCORD_BOT_TOKEN") or not os.environ.get("DISCORD_GUILD_ID"):
        print("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID not set in .env")
        return 1
    if not z_brain.is_configured():
        print("ANTHROPIC_API_KEY not set in .env")
        return 1

    results = run_review(args.count)
    if not results:
        return 1

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(_HERE, "z_review_runs")
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    prompt = build_handoff_prompt(results)
    prompt_path = os.path.join(out_dir, f"{stamp}.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    print(f"Saved review to {json_path}\n")

    if args.no_handoff:
        print("--no-handoff set, skipping Claude Code. Prompt saved at:", prompt_path)
        return 0

    print("Handing off to Claude Code...\n")
    if not hand_off_to_claude_code(prompt):
        print(
            "Couldn't find/run the `claude` CLI on this machine. Paste the "
            f"prompt below (also saved at {prompt_path}) into your Claude "
            "Code session:\n"
        )
        print(prompt)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
