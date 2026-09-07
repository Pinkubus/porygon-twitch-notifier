"""
reaction_roles.py — sync logic: diff current reactors against last-known
state and grant/revoke the mapped role for anyone who reacted/un-reacted.
"""
from __future__ import annotations

import os
import json
import logging

import discord_roles

logger = logging.getLogger("porygon.reaction_roles")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reaction_state.json")


def load_state() -> dict[str, list[str]]:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load reaction state: {e}")
        return {}


def save_state(state: dict[str, list[str]]):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_configured() -> bool:
    return bool(
        os.environ.get("DISCORD_BOT_TOKEN")
        and os.environ.get("DISCORD_GUILD_ID")
        and os.environ.get("DISCORD_REACTION_CHANNEL_ID")
        and os.environ.get("DISCORD_REACTION_MESSAGE_ID")
        and discord_roles.get_role_map()
    )


def sync(bot_user_id: str) -> bool:
    """Grant/revoke roles based on current reactions. Returns True if state changed."""
    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = os.environ["DISCORD_GUILD_ID"]
    channel_id = os.environ["DISCORD_REACTION_CHANNEL_ID"]
    message_id = os.environ["DISCORD_REACTION_MESSAGE_ID"]
    role_map = discord_roles.get_role_map()

    state = load_state()
    changed = False

    for emoji, role_id in role_map.items():
        current = set(discord_roles.get_reaction_users(channel_id, message_id, emoji, token))
        current.discard(bot_user_id)
        previous = set(state.get(emoji, []))

        for user_id in current - previous:
            if discord_roles.add_member_role(guild_id, user_id, role_id, token):
                logger.info(f"Granted role {role_id} ({emoji}) to {user_id}")
        for user_id in previous - current:
            if discord_roles.remove_member_role(guild_id, user_id, role_id, token):
                logger.info(f"Revoked role {role_id} ({emoji}) from {user_id}")

        if current != previous:
            changed = True
            state[emoji] = sorted(current)

    if changed:
        save_state(state)
    return changed
