"""
notifier.py — "Went live" Discord notifications for Twitch channels.

Designed to run as a single one-shot check under GitHub Actions (triggered on
a schedule/cron), rather than as a long-running polling loop. Each run:
  1. Refreshes a Twitch user access token from a stored refresh token.
  2. Checks whether any configured channel is live.
  3. Posts a Discord embed (as "Porygon") for any offline->live transition.
  4. Persists live/offline state to state.json so the workflow can commit it
     back to the repo, keeping state across runs.

Auth: Twitch OAuth Device Code Grant Flow (Public client, no client secret).
Public-client refresh tokens expire 30 days after they're issued, regardless
of use, so this will need re-authorizing roughly monthly. See README.md.

Required env vars (set as GitHub Actions secrets):
    TWITCH_CLIENT_ID
    TWITCH_REFRESH_TOKEN
    TWITCH_STREAMS_WEBHOOK_URL

Optional:
    TWITCH_CHANNELS  — comma-separated logins (default: the 3 below)
"""
from __future__ import annotations

import os
import sys
import json
import time
import logging
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("porygon.notifier")

_DEFAULT_CHANNELS = ["fondlyregarded", "erodite", "poogbooklet"]
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

_TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_TWITCH_API = "https://api.twitch.tv/helix"
_BOT_NAME = "Porygon"


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load state: {e}")
        return {}


def _save_state(state: dict):
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _get_channels() -> list[str]:
    raw = os.environ.get("TWITCH_CHANNELS", "")
    if raw.strip():
        return [c.strip().lower() for c in raw.split(",") if c.strip()]
    return _DEFAULT_CHANNELS


def _get_access_token() -> Optional[str]:
    """Public clients can refresh without a client secret."""
    client_id = os.environ.get("TWITCH_CLIENT_ID", "")
    refresh_token = os.environ.get("TWITCH_REFRESH_TOKEN", "")
    if not client_id or not refresh_token:
        logger.error("TWITCH_CLIENT_ID / TWITCH_REFRESH_TOKEN not set")
        return None

    resp = requests.post(_TWITCH_TOKEN_URL, data={
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
    return resp.json()["access_token"]


def _get_live_streams(client_id: str, token: str, channels: list[str]) -> dict[str, dict]:
    params = [("user_login", c) for c in channels]
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    resp = requests.get(f"{_TWITCH_API}/streams", params=params, headers=headers, timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Twitch streams fetch failed {resp.status_code}: {resp.text[:200]}")
        return {}
    data = resp.json().get("data", [])
    return {s["user_login"].lower(): s for s in data}


def _get_user_avatars(client_id: str, token: str, channels: list[str]) -> dict[str, str]:
    if not channels:
        return {}
    params = [("login", c) for c in channels]
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    resp = requests.get(f"{_TWITCH_API}/users", params=params, headers=headers, timeout=10)
    if resp.status_code != 200:
        return {}
    data = resp.json().get("data", [])
    return {u["login"].lower(): u.get("profile_image_url", "") for u in data}


def _post_live_notification(login: str, stream: dict, avatar_url: str = ""):
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

    payload = {"username": _BOT_NAME, "embeds": [embed]}
    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code not in (200, 204):
        logger.warning(f"Discord webhook post failed {resp.status_code}: {resp.text[:200]}")


def main() -> int:
    client_id = os.environ.get("TWITCH_CLIENT_ID", "")
    token = _get_access_token()
    if not token:
        return 1

    channels = _get_channels()
    state = _load_state()
    live = _get_live_streams(client_id, token, channels)

    newly_live = [c for c in channels if c in live and not state.get(c, {}).get("live")]
    avatars = _get_user_avatars(client_id, token, newly_live)

    for c in channels:
        is_live = c in live
        was_live = state.get(c, {}).get("live", False)
        if is_live and not was_live:
            logger.info(f"{c} just went live — notifying")
            _post_live_notification(c, live[c], avatars.get(c, ""))
        state[c] = {"live": is_live, "stream_id": live.get(c, {}).get("id", "")}

    _save_state(state)
    logger.info(f"Checked {len(channels)} channel(s), {len(live)} live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
