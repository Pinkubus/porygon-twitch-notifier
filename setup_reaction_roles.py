"""
setup_reaction_roles.py — one-time local script: posts the reaction-role
embed into DISCORD_REACTION_CHANNEL_ID and seeds it with one reaction per
emoji in REACTION_ROLE_MAP, so members just click to react.

Run locally (not in CI) after creating the roles and setting:
  DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_REACTION_CHANNEL_ID,
  REACTION_ROLE_MAP  (JSON: {"emoji": "role_id", ...})

Prints the resulting message ID — paste it into the DISCORD_REACTION_MESSAGE_ID
repo variable so loop.py knows which message to poll.
"""
from __future__ import annotations

import os
import sys
import time

import discord_roles


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    guild_id = os.environ.get("DISCORD_GUILD_ID", "")
    channel_id = os.environ.get("DISCORD_REACTION_CHANNEL_ID", "")
    role_map = discord_roles.get_role_map()

    if not token or not guild_id or not channel_id or not role_map:
        print("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID / DISCORD_REACTION_CHANNEL_ID / "
              "REACTION_ROLE_MAP must all be set.")
        return 1

    roles = {r["id"]: r["name"] for r in discord_roles.get_guild_roles(guild_id, token)}

    lines = [f"{emoji} — **{roles.get(role_id, role_id)}**" for emoji, role_id in role_map.items()]
    embed = {
        "title": "Pick your roles!",
        "description": "React below to get pinged for topics you're into. "
                        "Remove your reaction to remove the role.\n\n" + "\n".join(lines),
        "color": 0x9146FF,
    }

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
