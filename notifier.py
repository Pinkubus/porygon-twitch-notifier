"""
notifier.py — single one-shot Twitch live-check (manual/local testing).

The GitHub Actions workflow uses loop.py for continuous ~30s-latency
monitoring; this script is kept for quick manual checks, e.g.:

    set TWITCH_CLIENT_ID=... & set TWITCH_REFRESH_TOKEN=... & ^
    set TWITCH_STREAMS_WEBHOOK_URL=... & python notifier.py

It does not persist a rotated refresh token anywhere — if Twitch rotates it
during a manual run, re-run authorize.py to get a fresh one.
"""
from __future__ import annotations

import os
import sys
import json
import logging

import twitch_api

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("porygon.notifier")

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


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


def main() -> int:
    client_id = os.environ.get("TWITCH_CLIENT_ID", "")
    refresh_token = os.environ.get("TWITCH_REFRESH_TOKEN", "")
    if not client_id or not refresh_token:
        logger.error("TWITCH_CLIENT_ID / TWITCH_REFRESH_TOKEN not set")
        return 1

    tokens = twitch_api.refresh_access_token(client_id, refresh_token)
    if not tokens:
        return 1
    token = tokens["access_token"]

    channels = twitch_api.get_channels()
    state = _load_state()
    live, _ = twitch_api.get_live_streams(client_id, token, channels)

    newly_live = [c for c in channels if c in live and not state.get(c, {}).get("live")]
    avatars = twitch_api.get_user_avatars(client_id, token, newly_live)

    for c in channels:
        is_live = c in live
        was_live = state.get(c, {}).get("live", False)
        if is_live and not was_live:
            logger.info(f"{c} just went live — notifying")
            twitch_api.post_live_notification(c, live[c], avatars.get(c, ""))
        state[c] = {"live": is_live, "stream_id": live.get(c, {}).get("id", "")}

    _save_state(state)
    logger.info(f"Checked {len(channels)} channel(s), {len(live)} live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

