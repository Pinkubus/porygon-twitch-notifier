"""
reconcile_reactions.py — one-off diagnostic + repair for the reaction-roles
feature. Unlike reaction_roles.sync() (which only diffs against the last
saved reaction_state.json), this compares live Discord reactions against
each reactor's ACTUAL current roles, so it self-heals from any past bug or
drift instead of trusting the local cache. Grants any missing roles, then
rewrites reaction_state.json to match ground truth.

Run via the "Reconcile reaction roles" workflow_dispatch, or locally with
DISCORD_BOT_TOKEN / DISCORD_GUILD_ID / DISCORD_REACTION_CHANNEL_ID /
DISCORD_REACTION_MESSAGE_ID / REACTION_ROLE_MAP set.
"""
from __future__ import annotations

import os
import sys

import discord_roles
import reaction_roles


def main() -> int:
    token = os.environ["DISCORD_BOT_TOKEN"]
    guild_id = os.environ["DISCORD_GUILD_ID"]
    channel_id = os.environ["DISCORD_REACTION_CHANNEL_ID"]
    message_id = os.environ["DISCORD_REACTION_MESSAGE_ID"]
    role_map = discord_roles.get_role_map()
    bot_user_id = discord_roles.get_bot_user_id(token)

    # emoji -> reactor ids (ground truth from Discord)
    reactors_by_emoji: dict[str, set[str]] = {}
    expected_roles: dict[str, set[str]] = {}  # user_id -> role_ids they should have
    for emoji, role_id in role_map.items():
        reactors = set(discord_roles.get_reaction_users(channel_id, message_id, emoji, token))
        reactors.discard(bot_user_id)
        reactors_by_emoji[emoji] = reactors
        for user_id in reactors:
            expected_roles.setdefault(user_id, set()).add(role_id)

    granted = 0
    for user_id, want_roles in expected_roles.items():
        member = discord_roles.get_member_roles(guild_id, user_id, token)
        if member is None:
            print(f"{user_id}: could not fetch member (left server?) — skipping")
            continue
        missing = want_roles - member
        for role_id in missing:
            ok = discord_roles.add_member_role(guild_id, user_id, role_id, token)
            print(f"{user_id}: granting missing role {role_id} -> {'ok' if ok else 'FAILED'}")
            if ok:
                granted += 1

    state = {emoji: sorted(users) for emoji, users in reactors_by_emoji.items()}
    reaction_roles.save_state(state)
    print(f"Reconcile complete: {granted} missing role(s) granted, reaction_state.json rewritten.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
