"""
twitch_api.py — shared Twitch/Discord helpers used by notifier.py and loop.py.
"""
from __future__ import annotations

import os
import time
import logging
from typing import Optional

import requests

logger = logging.getLogger("porygon.twitch_api")

DEFAULT_CHANNELS = ["fondlyregarded", "erodite", "poogbooklet", "onepuffman"]
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API = "https://api.twitch.tv/helix"
BOT_NAME = "Porygon"


def get_channels() -> list[str]:
    raw = os.environ.get("TWITCH_CHANNELS", "")
    if raw.strip():
        return [c.strip().lower() for c in raw.split(",") if c.strip()]
    return DEFAULT_CHANNELS


def refresh_access_token(client_id: str, refresh_token: str) -> Optional[dict]:
    """Public clients can refresh without a client secret."""
    resp = requests.post(TWITCH_TOKEN_URL, data={
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=10)
    if resp.status_code != 200:
        logger.error(
            f"Twitch token refresh failed {resp.status_code}: {resp.text[:200]} "
            "— the refresh token may have expired (30-day limit for Public "
            "clients). Re-run authorize.py and update the TWITCH_REFRESH_TOKEN secret."
        )
        return None
    return resp.json()


def get_live_streams(client_id: str, token: str, channels: list[str]) -> tuple[dict, int]:
    """Return ({login: stream_info}, http_status)."""
    params = [("user_login", c) for c in channels]
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    resp = requests.get(f"{TWITCH_API}/streams", params=params, headers=headers, timeout=10)
    if resp.status_code != 200:
        if resp.status_code != 401:
            logger.warning(f"Twitch streams fetch failed {resp.status_code}: {resp.text[:200]}")
        return {}, resp.status_code
    data = resp.json().get("data", [])
    return {s["user_login"].lower(): s for s in data}, 200


def get_user_avatars(client_id: str, token: str, channels: list[str]) -> dict[str, str]:
    if not channels:
        return {}
    params = [("login", c) for c in channels]
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    resp = requests.get(f"{TWITCH_API}/users", params=params, headers=headers, timeout=10)
    if resp.status_code != 200:
        return {}
    data = resp.json().get("data", [])
    return {u["login"].lower(): u.get("profile_image_url", "") for u in data}


def post_live_notification(login: str, stream: dict, avatar_url: str = ""):
    webhook_url = os.environ.get("TWITCH_STREAMS_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("TWITCH_STREAMS_WEBHOOK_URL not configured — skipping notification")
        return

    title = (stream.get("title") or "").strip() or "(no title)"
    game = stream.get("game_name") or "Unknown"
    thumb = (stream.get("thumbnail_url") or "").replace("{width}", "440").replace("{height}", "248")

    embed = {
        "title": f"🔴 {login} is now live on Twitch!",
        "description": title,
        "url": f"https://twitch.tv/{login}",
        "color": 0x9146FF,  # Twitch purple
        "fields": [{"name": "Playing", "value": game, "inline": True}],
    }
    if thumb:
        embed["image"] = {"url": f"{thumb}?t={int(time.time())}"}  # cache-bust
    if avatar_url:
        embed["thumbnail"] = {"url": avatar_url}

    payload = {"username": BOT_NAME, "embeds": [embed]}
    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code not in (200, 204):
        logger.warning(f"Discord webhook post failed {resp.status_code}: {resp.text[:200]}")
