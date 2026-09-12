"""
loop.py — continuous Twitch live-check loop, for GitHub Actions.

Checks every ~30 seconds instead of relying on the `schedule` trigger's
5-minute-minimum cron granularity. GitHub Actions jobs have a hard 6-hour
limit, so this exits cleanly after MAX_RUN_SECONDS and the workflow's cron
trigger starts a fresh job well before the previous one ends, keeping
coverage continuous (see .github/workflows/notify.yml).

Rotated refresh tokens and live/offline state changes are persisted
immediately (not just at job end) so a killed/cancelled job loses as
little as possible.
"""
from __future__ import annotations

import os
import sys
import json
import time
import logging
import subprocess

import activity_log
import twitch_api
import discord_roles
import reaction_roles
import quotes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("porygon.loop")

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
_POLL_SECONDS = 30
_DEFAULT_MAX_RUN_SECONDS = 5 * 3600 + 55 * 60  # 5h55m, just under the 6h Actions job limit


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load state: {e}")
        return {}


def _persist_refresh_token(new_token: str):
    """Best-effort: never raises, so a failed persist can't discard the new
    token from the caller's in-memory state (that would strand the process
    on an already-rotated, now-invalid refresh_token)."""
    repo = os.environ["GITHUB_REPOSITORY"]
    for attempt in range(3):
        result = subprocess.run(
            ["gh", "secret", "set", "TWITCH_REFRESH_TOKEN", "--repo", repo],
            input=new_token, text=True,
        )
        if result.returncode == 0:
            logger.info("Rotated refresh token persisted to secret")
            return
        logger.warning(f"gh secret set failed (attempt {attempt + 1}/3)")
        time.sleep(2)
    logger.error("CRITICAL: failed to persist rotated refresh token after retries — "
                 "the next scheduled run will fail until this secret is fixed")
    _alert_discord(
        "Failed to persist a rotated Twitch refresh token after 3 retries. "
        "The next scheduled run will likely fail with an invalid refresh token — "
        "check `gh secret set` / GH_PAT permissions."
    )


def _alert_discord(message: str):
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_REACTION_CHANNEL_ID")
    if not token or not channel_id:
        return
    try:
        discord_roles.post_message(channel_id, token, {"description": f"▽△PORYGON▽△ fainted!\n{message}"})
    except Exception as e:
        logger.warning(f"Failed to post Discord alert: {e}")


def _commit_files(paths: list[str], message: str):
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return
    subprocess.run(["git", "add", *paths], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return  # nothing staged
    subprocess.run(["git", "commit", "-m", message], check=True)
    for _ in range(3):
        if subprocess.run(["git", "push"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "--rebase"])
    logger.warning(f"Failed to push {paths} after retries")


def _save_state_and_commit(state: dict):
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    _commit_files(["state.json"], "Update live/offline state [skip ci]")


def _refresh(client_id: str, refresh_token: str) -> tuple[str, str] | None:
    """Refresh the access token, persisting a rotated refresh token if issued."""
    tokens = twitch_api.refresh_access_token(client_id, refresh_token)
    if not tokens:
        return None
    new_refresh = tokens.get("refresh_token") or refresh_token
    if new_refresh != refresh_token:
        _persist_refresh_token(new_refresh)
    return tokens["access_token"], new_refresh


def main() -> int:
    client_id = os.environ.get("TWITCH_CLIENT_ID", "")
    refresh_token = os.environ.get("TWITCH_REFRESH_TOKEN", "")
    if not client_id or not refresh_token:
        logger.error("TWITCH_CLIENT_ID / TWITCH_REFRESH_TOKEN not set")
        return 1

    refreshed = _refresh(client_id, refresh_token)
    if not refreshed:
        _alert_discord("Twitch refresh token is invalid — run authorize.py and update TWITCH_REFRESH_TOKEN.")
        return 1
    access_token, refresh_token = refreshed

    max_run_seconds = int(os.environ.get("LOOP_MAX_SECONDS") or _DEFAULT_MAX_RUN_SECONDS)
    channels = twitch_api.get_channels()
    state = _load_state()
    logger.info(f"Starting loop: channels={channels}, poll={_POLL_SECONDS}s, "
                f"max_run={max_run_seconds}s")

    reaction_roles_enabled = reaction_roles.is_configured()
    quotes_enabled = quotes.is_configured()
    bot_user_id = None
    if reaction_roles_enabled or quotes_enabled:
        bot_user_id = discord_roles.get_bot_user_id(os.environ["DISCORD_BOT_TOKEN"])
        reaction_roles_enabled = reaction_roles_enabled and bot_user_id is not None
        quotes_enabled = quotes_enabled and bot_user_id is not None
        logger.info(f"Reaction roles: {'enabled' if reaction_roles_enabled else 'disabled (setup incomplete)'}")
        logger.info(f"Quotes: {'enabled' if quotes_enabled else 'disabled (setup incomplete)'}")

    start = time.time()
    while time.time() - start < max_run_seconds:
        loop_start = time.time()

        try:
            live, status = twitch_api.get_live_streams(client_id, access_token, channels)
            if status == 401:
                refreshed = _refresh(client_id, refresh_token)
                if not refreshed:
                    logger.error("Re-auth required — refresh token invalid")
                    _alert_discord("Twitch refresh token is invalid — run authorize.py and update TWITCH_REFRESH_TOKEN.")
                    return 1
                access_token, refresh_token = refreshed
                live, status = twitch_api.get_live_streams(client_id, access_token, channels)

            newly_live = [c for c in channels if c in live and not state.get(c, {}).get("live")]
            state_changed = any(
                (c in live) != state.get(c, {}).get("live", False) for c in channels
            )
            avatars = twitch_api.get_user_avatars(client_id, access_token, newly_live)

            for c in channels:
                is_live = c in live
                was_live = state.get(c, {}).get("live", False)
                if is_live and not was_live:
                    logger.info(f"{c} just went live — notifying")
                    twitch_api.post_live_notification(c, live[c], avatars.get(c, ""))
                state[c] = {"live": is_live, "stream_id": live.get(c, {}).get("id", "")}

            if state_changed:
                _save_state_and_commit(state)

            if reaction_roles_enabled and reaction_roles.sync(bot_user_id):
                _commit_files(["reaction_state.json"], "Update reaction-role state [skip ci]")

            if quotes_enabled and quotes.scan_and_process(bot_user_id):
                _commit_files(["quotes.json", "quotes_scan_state.json"], "Update quotes [skip ci]")

            if activity_log.flush_if_dirty():
                _commit_files(["activity.log"], "Update activity log [skip ci]")
        except Exception as e:
            # A transient network/API hiccup shouldn't kill a multi-hour job — log and retry next cycle.
            logger.warning(f"Cycle failed, will retry next cycle: {e}")

        elapsed = time.time() - loop_start
        time.sleep(max(0.0, _POLL_SECONDS - elapsed))

    logger.info("Max run time reached — exiting cleanly for the next scheduled run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
