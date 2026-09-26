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


def configured_guild_ids() -> list[str]:
    """DISCORD_GUILD_ID plus the optional second server
    (EXTRA_DISCORD_GUILD_ID) — every guild-scanning feature (quotes.py,
    porygon_z.py) loops over this instead of a single hardcoded guild."""
    ids = [os.environ["DISCORD_GUILD_ID"]]
    extra = os.environ.get("EXTRA_DISCORD_GUILD_ID", "").strip()
    if extra:
        ids.append(extra)
    return ids


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


def delete_channel(channel_id: str, token: str) -> bool:
    resp = _request("DELETE", f"{DISCORD_API}/channels/{channel_id}", token)
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else "error"
        logger.warning(f"Failed to delete channel {channel_id} {code}")
        return False
    return True


def create_text_channel(guild_id: str, token: str, name: str) -> Optional[dict]:
    resp = _request(
        "POST", f"{DISCORD_API}/guilds/{guild_id}/channels", token,
        json={"name": name, "type": 0},
    )
    if resp is None or resp.status_code not in (200, 201):
        code = resp.status_code if resp is not None else "error"
        body = resp.text[:300] if resp is not None else ""
        logger.warning(f"Failed to create channel {name!r} {code}: {body}")
        return None
    return resp.json()


def create_webhook(channel_id: str, token: str, name: str) -> Optional[dict]:
    """Includes the full "url" field (token and all), same as one made
    through Discord's own UI — usable immediately, no separate lookup."""
    resp = _request(
        "POST", f"{DISCORD_API}/channels/{channel_id}/webhooks", token,
        json={"name": name},
    )
    if resp is None or resp.status_code not in (200, 201):
        code = resp.status_code if resp is not None else "error"
        body = resp.text[:300] if resp is not None else ""
        logger.warning(f"Failed to create webhook {name!r} {code}: {body}")
        return None
    data = resp.json()
    data["url"] = f"https://discord.com/api/webhooks/{data['id']}/{data['token']}"
    return data


def open_dm_channel(user_id: str, token: str) -> Optional[str]:
    resp = _request("POST", f"{DISCORD_API}/users/@me/channels", token, json={"recipient_id": user_id})
    data = _json(resp, f"DM channel with {user_id}", None)
    return data["id"] if data else None


def create_guild_emoji(guild_id: str, token: str, name: str, image_data_uri: str) -> Optional[dict]:
    """Upload a custom emoji. `image_data_uri` is a data: URI (Discord's required
    format), e.g. "data:image/gif;base64,....". Image must be <= 256KB."""
    resp = requests.post(
        f"{DISCORD_API}/guilds/{guild_id}/emojis",
        headers=_headers(token),
        json={"name": name, "image": image_data_uri},
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        logger.warning(f"Failed to create emoji {name!r} {resp.status_code}: {resp.text[:300]}")
        return None
    return resp.json()


_channel_cache: dict[str, tuple[float, list[dict]]] = {}
_CHANNEL_TTL = int(os.environ.get("DISCORD_CHANNEL_CACHE_SECONDS", 300))

# Per-channel message cache. quotes and porygon_z both sweep every channel on
# the same cycle with their own cursors, so without this the whole server gets
# fetched twice. Messages after a later cursor are a subset of messages after
# an earlier one, so one fetch can serve both.
_message_cache: dict[str, tuple[float, Optional[str], int, list[dict]]] = {}
_MESSAGE_TTL = float(os.environ.get("DISCORD_MESSAGE_CACHE_SECONDS", 8))


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
    before: Optional[str] = None, around: Optional[str] = None, limit: int = 100,
) -> list[dict]:
    """Raw message objects (newest-first), per Discord's default ordering."""
    use_cache = before is None and around is None
    if use_cache:
        entry = _message_cache.get(channel_id)
        if (
            entry
            and time.time() - entry[0] < _MESSAGE_TTL
            and entry[2] >= limit
            and _covers(entry[1], after)
        ):
            return [m for m in entry[3] if after is None or int(m["id"]) > int(after)][:limit]

    params: dict = {"limit": limit}
    if after:
        params["after"] = after
    if before:
        params["before"] = before
    if around:
        params["around"] = around
    messages = _json(
        _request("GET", f"{DISCORD_API}/channels/{channel_id}/messages", token, params=params),
        f"messages for {channel_id}", [],
    )
    if use_cache and messages is not None:
        _message_cache[channel_id] = (time.time(), after, limit, messages)
    return messages


def _covers(cached_after: Optional[str], wanted_after: Optional[str]) -> bool:
    """True if a fetch from `cached_after` already contains everything after
    `wanted_after` — i.e. the cached cursor is at or behind the wanted one."""
    if cached_after is None:
        return True
    if wanted_after is None:
        return False
    return int(cached_after) <= int(wanted_after)


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


def post_poll(
    channel_id: str, token: str, question: str, answers: list[str],
    duration_hours: int = 24, allow_multiselect: bool = False,
    reply_to: Optional[str] = None,
) -> Optional[str]:
    """Post a native Discord poll (Discord renders it and tallies the votes).

    Caller is responsible for staying inside Discord's limits — 2-10 answers,
    300 chars of question, 55 chars per answer, duration in hours up to 32
    days; z_polls clamps to those before calling. Failures log the response
    body, since a rejected poll is almost always one bad field.
    """
    payload: dict = {
        "poll": {
            "question": {"text": question},
            "answers": [{"poll_media": {"text": answer}} for answer in answers],
            "duration": duration_hours,
            "allow_multiselect": allow_multiselect,
            "layout_type": 1,
        }
    }
    if reply_to:
        payload["message_reference"] = {
            "message_id": reply_to, "channel_id": channel_id, "fail_if_not_exists": False,
        }
        payload["allowed_mentions"] = {"parse": [], "replied_user": False}
    resp = _request("POST", f"{DISCORD_API}/channels/{channel_id}/messages", token, json=payload)
    if resp is None or resp.status_code not in (200, 201):
        code = resp.status_code if resp is not None else "error"
        body = resp.text[:300] if resp is not None else ""
        logger.warning(f"Failed to post poll {code}: {body}")
        return None
    return resp.json()["id"]


def post_poll_with_answer_ids(
    channel_id: str, token: str, question: str, answers: list[str],
    duration_hours: int = 24,
) -> Optional[tuple[str, dict[str, int]]]:
    """Same as post_poll, but also returns {answer_text: answer_id} from
    Discord's own response — needed to later ask "who voted for answer N"
    (social_backup.py's `!post` disambiguation) instead of just displaying
    the poll and forgetting about it."""
    payload = {
        "poll": {
            "question": {"text": question},
            "answers": [{"poll_media": {"text": answer}} for answer in answers],
            "duration": duration_hours,
            "allow_multiselect": False,
            "layout_type": 1,
        }
    }
    resp = _request("POST", f"{DISCORD_API}/channels/{channel_id}/messages", token, json=payload)
    if resp is None or resp.status_code not in (200, 201):
        code = resp.status_code if resp is not None else "error"
        body = resp.text[:300] if resp is not None else ""
        logger.warning(f"Failed to post poll {code}: {body}")
        return None
    data = resp.json()
    answer_ids = {
        a["poll_media"]["text"]: a["answer_id"]
        for a in data.get("poll", {}).get("answers", [])
        if "answer_id" in a
    }
    return data["id"], answer_ids


def get_poll_answer_counts(channel_id: str, message_id: str, token: str) -> Optional[dict[str, int]]:
    """Live vote counts for a poll, keyed by answer text.

    Discord only marks results finalized once the poll closes, but the running
    counts are there the whole time, which is what a mid-poll check wants. One
    GET covers every answer — asking per-answer (get_poll_answer_voters) costs
    a request each and returns voter ids nobody needs here. None means the
    message or its poll is gone; {} means it's there with nothing voted yet.
    """
    message = get_message(channel_id, message_id, token)
    poll = (message or {}).get("poll")
    if not poll:
        return None
    texts = {
        answer.get("answer_id"): ((answer.get("poll_media") or {}).get("text") or "")
        for answer in poll.get("answers") or []
    }
    counts = {text: 0 for text in texts.values() if text}
    for entry in (poll.get("results") or {}).get("answer_counts") or []:
        text = texts.get(entry.get("id"))
        if text:
            counts[text] = int(entry.get("count") or 0)
    return counts


def get_poll_answer_voters(channel_id: str, message_id: str, answer_id: int, token: str) -> list[str]:
    """User ids who voted for one answer of a poll (Discord has no
    gateway-free way to be notified of a vote, so callers poll this)."""
    resp = _request(
        "GET", f"{DISCORD_API}/channels/{channel_id}/polls/{message_id}/answers/{answer_id}",
        token, params={"limit": 100},
    )
    if resp is None or resp.status_code != 200:
        return []
    return [u["id"] for u in resp.json().get("users", [])]


def post_message(channel_id: str, token: str, embed: dict, content: Optional[str] = None) -> Optional[str]:
    payload: dict = {"embeds": [embed]}
    if content:
        payload["content"] = content
    resp = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=_headers(token), json=payload, timeout=10,
    )
    if resp.status_code not in (200, 201):
        logger.warning(f"Failed to post message {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()["id"]


def send_dm(user_id: str, token: str, content: str) -> bool:
    """Opens (or reuses) a DM channel with `user_id` and sends `content`."""
    resp = _request(
        "POST", f"{DISCORD_API}/users/@me/channels", token,
        json={"recipient_id": user_id},
    )
    dm_channel = _json(resp, f"DM channel with {user_id}", None)
    if not dm_channel:
        return False
    resp = _request(
        "POST", f"{DISCORD_API}/channels/{dm_channel['id']}/messages", token,
        json={"content": content},
    )
    if resp is None or resp.status_code not in (200, 201):
        code = resp.status_code if resp is not None else "error"
        logger.warning(f"Failed to DM {user_id} {code}")
        return False
    return True


def post_webhook(
    webhook_url: str, content: Optional[str] = None,
    embeds: Optional[list[dict]] = None, username: Optional[str] = None,
) -> bool:
    """Post directly to a Discord webhook URL, bypassing the bot-token path
    the rest of this module uses — for mirroring into a server the bot
    account isn't a member of. Never raises (a mirror failing shouldn't be
    able to take down the primary post it's piggy-backing on)."""
    payload: dict = {}
    if username:
        payload["username"] = username
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
    except requests.RequestException as e:
        logger.warning(f"Webhook post failed: {e}")
        return False
    if resp.status_code not in (200, 204):
        logger.warning(f"Webhook post failed {resp.status_code}: {resp.text[:200]}")
        return False
    return True


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


def pin_message(channel_id: str, message_id: str, token: str) -> bool:
    """Pin a message the bot posted. Needs Manage Messages in that channel."""
    resp = _request_with_rate_limit_retry(
        requests.put, f"{DISCORD_API}/channels/{channel_id}/pins/{message_id}", token,
    )
    if resp.status_code not in (200, 204):
        logger.warning(f"Failed to pin message {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def remove_own_reaction(channel_id: str, message_id: str, emoji: str, token: str) -> bool:
    resp = _request_with_rate_limit_retry(
        requests.delete, f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me", token,
    )
    if resp.status_code != 204:
        logger.warning(f"Failed to remove reaction {emoji} {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def remove_user_reaction(channel_id: str, message_id: str, emoji: str, user_id: str, token: str) -> bool:
    """Remove someone else's reaction. Needs Manage Messages."""
    resp = _request_with_rate_limit_retry(
        requests.delete,
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/{user_id}", token,
    )
    if resp.status_code != 204:
        logger.warning(f"Failed to remove {user_id}'s reaction {emoji} {resp.status_code}: {resp.text[:200]}")
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
