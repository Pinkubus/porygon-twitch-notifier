"""
quotes_watch_local.py — fast local quote-scan loop, for near-real-time
`porygonwow` reactions without waiting on GitHub Actions' cron granularity.

GitHub Actions can't poll more often than every 5 minutes (discord_sync.yml)
or run past 6h at a time (notify.yml's Twitch loop). Run this locally
instead when you want quotes reacted to within ~15s: it just calls
quotes.scan_and_process() every POLL_SECONDS and pushes any changes, same as
loop.py does, but on a much tighter cycle since it isn't sharing time with
Twitch checks or paying GitHub Actions' runner-startup overhead.

Setup: same DISCORD_BOT_TOKEN/DISCORD_GUILD_ID as the repo's GH
secret/variable, in this folder's .env (gitignored).

Run:
    python quotes_watch_local.py
"""
from __future__ import annotations

import os
import sys
import subprocess
import time
import logging

import activity_log
import discord_roles
import quotes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("porygon.quotes_watch_local")

_HERE = os.path.dirname(os.path.abspath(__file__))
_POLL_SECONDS = 10


def _load_env_file(path: str):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _commit_and_push(paths: list[str], message: str):
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return
    subprocess.run(["git", "-C", _HERE, "add", *paths], check=True)
    if subprocess.run(["git", "-C", _HERE, "diff", "--cached", "--quiet"]).returncode == 0:
        return  # nothing staged
    subprocess.run(["git", "-C", _HERE, "commit", "-m", message], check=True)
    for _ in range(3):
        if subprocess.run(["git", "-C", _HERE, "push"]).returncode == 0:
            return
        subprocess.run(["git", "-C", _HERE, "pull", "--rebase"])
    logger.warning(f"Failed to push {paths} after retries")


def main() -> int:
    _load_env_file(os.path.join(_HERE, ".env"))

    if not quotes.is_configured():
        logger.error("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID not set in .env")
        return 1

    bot_user_id = discord_roles.get_bot_user_id(os.environ["DISCORD_BOT_TOKEN"])
    if bot_user_id is None:
        logger.error("Failed to resolve bot user id — check DISCORD_BOT_TOKEN")
        return 1

    logger.info(f"Watching quotes every {_POLL_SECONDS}s. Ctrl+C to stop.")
    while True:
        cycle_start = time.time()
        try:
            if quotes.scan_and_process(bot_user_id):
                _commit_and_push(
                    ["quotes.json", "quotes_scan_state.json"], "Update quotes [skip ci]",
                )
            if activity_log.flush_if_dirty():
                _commit_and_push(["activity.log"], "Update activity log [skip ci]")
        except Exception as e:
            logger.warning(f"Cycle failed, will retry next cycle: {e}")

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, _POLL_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
