"""
setup_reaction_roles.py — one-time local script: posts the reaction-role
embed into DISCORD_REACTION_CHANNEL_ID and seeds it with one reaction per
emoji in REACTION_ROLE_MAP, so members just click to react.

Run locally (not in CI) after creating the roles and setting:
  DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_REACTION_CHANNEL_ID,
  REACTION_ROLE_MAP  (JSON: {"emoji": "role_id", ...})

Prints the resulting message ID — paste it into the DISCORD_REACTION_MESSAGE_ID
repo variable so loop.py knows which message to poll.

If DISCORD_REACTION_MESSAGE_ID is already set, edits that existing message's
embed in place instead of posting (and skips re-seeding reactions).
"""
from __future__ import annotations

import os
import sys
import time

import discord_roles

# emoji -> channel id(s) the role is meant for, shown as "- #channel" hints.
ROLE_CHANNELS = {
    "🎮": ["1439993682697912400"],  # vidya-general
    "🎨": ["1478887863176138935"],  # arts-and-crafts
    "🍲": ["1504197674935652592"],  # yummy-posting
    "💻": ["1533645858606944256"],  # techno-logiaaa
    "🎵": ["1507442588070973621"],  # music
    "🎬": ["1520144435420074077"],  # movies
    "🌸": ["1504124429938987218"],  # anime-and-manga
    "🐈": ["1450534137285967973"],  # cat-kingdom
    "🎪": ["1470092267909021830", "1533891168377766051"],  # cosplay-idea-dump, fitches-con
    "🤝": ["1437453865925738616", "1533891168377766051"],  # meet-ups, fitches-con
    "🃏": ["1436946294282129411"],  # memes
}


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    guild_id = os.environ.get("DISCORD_GUILD_ID", "")
    channel_id = os.environ.get("DISCORD_REACTION_CHANNEL_ID", "")
    existing_message_id = os.environ.get("DISCORD_REACTION_MESSAGE_ID", "")
    role_map = discord_roles.get_role_map()

    if not token or not guild_id or not channel_id or not role_map:
        print("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID / DISCORD_REACTION_CHANNEL_ID / "
              "REACTION_ROLE_MAP must all be set.")
        return 1

    roles = {r["id"]: r["name"] for r in discord_roles.get_guild_roles(guild_id, token)}

    lines = []
    for emoji, role_id in role_map.items():
        line = f"{emoji} — **{roles.get(role_id, role_id)}**"
        channels = ROLE_CHANNELS.get(emoji)
        if channels:
            line += " - " + ", ".join(f"<#{cid}>" for cid in channels)
        lines.append(line)

    embed = {
        "title": "Pick your roles!",
        "description": "React below to get pinged for topics you're into. "
                        "Remove your reaction to remove the role.\n\n" + "\n".join(lines),
        "color": 0x9146FF,
    }

    if existing_message_id:
        if not discord_roles.edit_message(channel_id, existing_message_id, token, embed):
            print("Failed to edit the existing reaction-role message.")
            return 1
        print(f"Edited message {existing_message_id}.")
        return 0

    message_id = discord_roles.post_message(channel_id, token, embed)
    if not message_id:
        print("Failed to post the reaction-role message.")
        return 1

    for emoji in role_map:
        discord_roles.add_own_reaction(channel_id, message_id, emoji, token)
        time.sleep(0.5)  # stay well under Discord's rate limit

    print(f"Posted message {message_id} with {len(role_map)} reactions.")
    print("Set this as the DISCORD_REACTION_MESSAGE_ID repo variable:")
    print(message_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())

