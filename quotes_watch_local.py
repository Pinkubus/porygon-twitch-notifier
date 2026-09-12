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
import threading
import time
import logging

import activity_log
import discord_roles
import quotes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("porygon.quotes_watch_local")

_HERE = os.path.dirname(os.path.abspath(__file__))
_POLL_SECONDS = 10
_ICON_PATH = r"C:\Users\Williwaugh\Desktop\emotes\porygoncuter.png"
_GIT_TIMEOUT = 30

# Never let a stalled credential prompt (e.g. no saved credentials in this
# session, such as under a SYSTEM/no-login scheduled task) hang the loop.
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")


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


def _git(*args: str) -> int:
    try:
        return subprocess.run(["git", "-C", _HERE, *args], timeout=_GIT_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        logger.warning(f"git {' '.join(args)} timed out")
        return 1


def _commit_and_push(paths: list[str], message: str):
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return
    if _git("add", *paths) != 0:
        return
    if subprocess.run(
        ["git", "-C", _HERE, "diff", "--cached", "--quiet"], timeout=_GIT_TIMEOUT,
    ).returncode == 0:
        return  # nothing staged
    if _git("commit", "-m", message) != 0:
        return
    for _ in range(3):
        if _git("push") == 0:
            return
        _git("pull", "--rebase")
    logger.warning(f"Failed to push {paths} after retries")


def _poll_loop(bot_user_id: str, stop_event: threading.Event) -> None:
    logger.info(f"Watching quotes every {_POLL_SECONDS}s.")
    while not stop_event.is_set():
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
        stop_event.wait(max(0.0, _POLL_SECONDS - elapsed))


def _run_tray_icon(stop_event: threading.Event) -> None:
    # No desktop to attach to before login/in Session 0 — just skip the icon then.
    try:
        import pystray
        from PIL import Image

        image = Image.open(_ICON_PATH)
    except Exception as e:
        logger.warning(f"Tray icon unavailable, running without it: {e}")
        return

    def on_quit(icon, _item):
        stop_event.set()
        icon.stop()

    icon = pystray.Icon(
        "porygon", image, "Porygon quote watcher (running)",
        menu=pystray.Menu(pystray.MenuItem("Quit", on_quit)),
    )
    try:
        icon.run()
    except Exception as e:
        logger.warning(f"Tray icon failed, running without it: {e}")


def main() -> int:
    _load_env_file(os.path.join(_HERE, ".env"))

    if not quotes.is_configured():
        logger.error("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID not set in .env")
        return 1

    bot_user_id = discord_roles.get_bot_user_id(os.environ["DISCORD_BOT_TOKEN"])
    if bot_user_id is None:
        logger.error("Failed to resolve bot user id — check DISCORD_BOT_TOKEN")
        return 1

    stop_event = threading.Event()
    poll_thread = threading.Thread(target=_poll_loop, args=(bot_user_id, stop_event), daemon=True)
    poll_thread.start()

    # icon.run() blocks (owning the tray message loop) until Quit is clicked, or
    # returns immediately if there's no desktop to attach to right now.
    _run_tray_icon(stop_event)
    while poll_thread.is_alive() and not stop_event.is_set():
        poll_thread.join(1)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
