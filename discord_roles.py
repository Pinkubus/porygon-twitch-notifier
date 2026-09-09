"""
discord_roles.py — Discord bot API helpers for the reaction-role feature.

Uses DISCORD_BOT_TOKEN (a real bot account, not a webhook) so it can read
message reactions and grant/revoke guild roles. Separate from twitch_api.py,
which only ever posts via webhook.
"""
from __future__ import annotations

import os
import json
import logging
import time
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger("porygon.discord_roles")

DISCORD_API = "https://discord.com/api/v10"
# Discord's edge (Cloudflare) rejects requests with no/generic User-Agent.
USER_AGENT = "DiscordBot (https://github.com/Pinkubus/porygon-twitch-notifier, 1.0)"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}


def get_role_map() -> dict[str, str]:
    """emoji -> role_id, from the REACTION_ROLE_MAP repo variable (JSON)."""
    raw = os.environ.get("REACTION_ROLE_MAP", "")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Failed to parse REACTION_ROLE_MAP: {e}")
        return {}


def get_guild_roles(guild_id: str, token: str) -> list[dict]:
    resp = requests.get(f"{DISCORD_API}/guilds/{guild_id}/roles", headers=_headers(token), timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch guild roles {resp.status_code}: {resp.text[:200]}")
        return []
    return resp.json()


def get_guild_text_channels(guild_id: str, token: str) -> list[dict]:
    """Text channels (type 0) in the guild, for message-scanning features."""
    resp = requests.get(f"{DISCORD_API}/guilds/{guild_id}/channels", headers=_headers(token), timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch guild channels {resp.status_code}: {resp.text[:200]}")
        return []
    return [c for c in resp.json() if c.get("type") == 0]


def get_channel_messages(
    channel_id: str, token: str, after: Optional[str] = None,
    before: Optional[str] = None, limit: int = 100,
) -> list[dict]:
    """Raw message objects (newest-first), per Discord's default ordering."""
    params: dict = {"limit": limit}
    if after:
        params["after"] = after
    if before:
        params["before"] = before
    resp = requests.get(
        f"{DISCORD_API}/channels/{channel_id}/messages", headers=_headers(token), params=params, timeout=10,
    )
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch messages for {channel_id} {resp.status_code}: {resp.text[:200]}")
        return []
    return resp.json()


def get_bot_user_id(token: str) -> Optional[str]:
    resp = requests.get(f"{DISCORD_API}/users/@me", headers=_headers(token), timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch bot user id {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()["id"]


def get_member_roles(guild_id: str, user_id: str, token: str) -> Optional[set[str]]:
    resp = requests.get(f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}", headers=_headers(token), timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch member {user_id} {resp.status_code}: {resp.text[:200]}")
        return None
    return set(resp.json().get("roles", []))


def post_message(channel_id: str, token: str, embed: dict) -> Optional[str]:
    resp = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=_headers(token), json={"embeds": [embed]}, timeout=10,
    )
    if resp.status_code not in (200, 201):
        logger.warning(f"Failed to post message {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()["id"]


def edit_message(channel_id: str, message_id: str, token: str, embed: dict) -> bool:
    resp = requests.patch(
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
        headers=_headers(token), json={"embeds": [embed]}, timeout=10,
    )
    if resp.status_code != 200:
        logger.warning(f"Failed to edit message {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def add_own_reaction(channel_id: str, message_id: str, emoji: str, token: str) -> bool:
    resp = requests.put(
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me",
        headers=_headers(token), timeout=10,
    )
    if resp.status_code != 204:
        logger.warning(f"Failed to add reaction {emoji} {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def get_reaction_users(channel_id: str, message_id: str, emoji: str, token: str) -> list[str]:
    """Return user IDs who reacted with `emoji`, paginating past 100 if needed."""
    users: list[str] = []
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        resp = requests.get(
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}",
            headers=_headers(token), params=params, timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to fetch reactions {emoji} {resp.status_code}: {resp.text[:200]}")
            break
        page = resp.json()
        users.extend(u["id"] for u in page)
        if len(page) < 100:
            break
        after = page[-1]["id"]
    return users


def _request_with_rate_limit_retry(method, url: str, token: str) -> requests.Response:
    resp = method(url, headers=_headers(token), timeout=10)
    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 1.0)
        time.sleep(retry_after + 0.1)
        resp = method(url, headers=_headers(token), timeout=10)
    return resp


def add_member_role(guild_id: str, user_id: str, role_id: str, token: str) -> bool:
    resp = _request_with_rate_limit_retry(
        requests.put, f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}/roles/{role_id}", token,
    )
    if resp.status_code != 204:
        logger.warning(f"Failed to add role {role_id} to {user_id} {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def remove_member_role(guild_id: str, user_id: str, role_id: str, token: str) -> bool:
    resp = _request_with_rate_limit_retry(
        requests.delete, f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}/roles/{role_id}", token,
    )
    if resp.status_code != 204:
        logger.warning(f"Failed to remove role {role_id} from {user_id} {resp.status_code}: {resp.text[:200]}")
        return False
    return True
