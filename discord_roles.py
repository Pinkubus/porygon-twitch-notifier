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


_MAX_RETRIES = int(os.environ.get("DISCORD_MAX_RETRIES", 3))


def _request(method: str, url: str, token: str, **kwargs) -> Optional[requests.Response]:
    """Discord request that waits out 429s instead of dropping the call.

    The fast local watcher polls every channel every few seconds, so rate
    limits are routine rather than exceptional; Discord tells us exactly how
    long to wait, so honour it rather than losing the message.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.request(
                method, url, headers=_headers(token), timeout=15, **kwargs,
            )
        except requests.RequestException as e:
            logger.warning(f"{method} {url.split('/api/v10')[-1]} failed: {e}")
            return None
        if resp.status_code != 429:
            return resp
        wait = float(resp.headers.get("Retry-After") or 1.0)
        try:
            wait = float(resp.json().get("retry_after", wait))
        except Exception:
            pass
        if attempt == _MAX_RETRIES - 1:
            logger.warning(f"Rate limited on {url.split('/api/v10')[-1]}, out of retries")
            return resp
        logger.info(f"Rate limited, waiting {wait:.1f}s")
        time.sleep(min(wait, 10.0) + 0.1)
    return None


def _json(resp: Optional[requests.Response], what: str, default):
    if resp is None:
        return default
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch {what} {resp.status_code}: {resp.text[:200]}")
        return default
    return resp.json()


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


def get_guild_emojis(guild_id: str, token: str) -> list[dict]:
    resp = requests.get(f"{DISCORD_API}/guilds/{guild_id}/emojis", headers=_headers(token), timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch guild emojis {resp.status_code}: {resp.text[:200]}")
        return []
    return resp.json()


_channel_cache: dict[str, tuple[float, list[dict]]] = {}
_CHANNEL_TTL = int(os.environ.get("DISCORD_CHANNEL_CACHE_SECONDS", 300))


def get_guild_text_channels(guild_id: str, token: str) -> list[dict]:
    """Text channels (type 0) in the guild, for message-scanning features.
    Cached because every scan cycle asks for it and it almost never changes."""
    cached = _channel_cache.get(guild_id)
    if cached and time.time() - cached[0] < _CHANNEL_TTL:
        return cached[1]
    data = _json(
        _request("GET", f"{DISCORD_API}/guilds/{guild_id}/channels", token),
        "guild channels", None,
    )
    if data is None:
        return cached[1] if cached else []
    channels = [c for c in data if c.get("type") == 0]
    _channel_cache[guild_id] = (time.time(), channels)
    return channels


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
    return _json(
        _request("GET", f"{DISCORD_API}/channels/{channel_id}/messages", token, params=params),
        f"messages for {channel_id}", [],
    )


def get_bot_user_id(token: str) -> Optional[str]:
    resp = requests.get(f"{DISCORD_API}/users/@me", headers=_headers(token), timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch bot user id {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()["id"]


def get_message(channel_id: str, message_id: str, token: str) -> Optional[dict]:
    return _json(
        _request("GET", f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}", token),
        f"message {message_id}", None,
    )


def delete_message(channel_id: str, message_id: str, token: str) -> bool:
    resp = _request("DELETE", f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}", token)
    if resp is None or resp.status_code != 204:
        code = resp.status_code if resp is not None else "error"
        logger.warning(f"Failed to delete message {message_id} {code}")
        return False
    return True


def get_member_roles(guild_id: str, user_id: str, token: str) -> Optional[set[str]]:
    resp = requests.get(f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}", headers=_headers(token), timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch member {user_id} {resp.status_code}: {resp.text[:200]}")
        return None
    return set(resp.json().get("roles", []))


def post_message_with_file(channel_id: str, token: str, content: str, file_path: Optional[str] = None) -> Optional[str]:
    """Post as the real bot account (no webhook needed), optionally with one attached file."""
    payload = {"content": content}
    if file_path:
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=_headers(token),
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (os.path.basename(file_path), f.read())},
                timeout=20,
            )
    else:
        resp = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=_headers(token), json=payload, timeout=10,
        )
    if resp.status_code not in (200, 201):
        logger.warning(f"Failed to post message {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()["id"]



def post_reply(channel_id: str, message_id: str, token: str, content: str) -> Optional[str]:
    """Post a plain-text message as a reply to `message_id` (no @-ping)."""
    payload = {
        "content": content,
        "message_reference": {"message_id": message_id, "channel_id": channel_id, "fail_if_not_exists": False},
        "allowed_mentions": {"parse": [], "replied_user": False},
    }
    resp = _request("POST", f"{DISCORD_API}/channels/{channel_id}/messages", token, json=payload)
    if resp is None or resp.status_code not in (200, 201):
        code = resp.status_code if resp is not None else "error"
        logger.warning(f"Failed to post reply {code}")
        return None
    return resp.json()["id"]


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
    resp = _request_with_rate_limit_retry(
        requests.put, f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me", token,
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
        resp = _request_with_rate_limit_retry(
            requests.get,
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}",
            token, params=params,
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


def _request_with_rate_limit_retry(method, url: str, token: str, **kwargs) -> requests.Response:
    resp = method(url, headers=_headers(token), timeout=10, **kwargs)
    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 1.0)
        time.sleep(retry_after + 0.1)
        resp = method(url, headers=_headers(token), timeout=10, **kwargs)
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
