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
import json
import logging

# Windows' default console/redirect encoding is the system ANSI codepage
# (cp1252, reported as "'charmap' codec"), not UTF-8 — and Discord channel
# names and message content here are full of emoji. Any print()/logger call
# that includes one throws UnicodeEncodeError the moment stdout/stderr isn't
# a real UTF-8-capable console (piped to a file via `>`, run under a
# scheduled task, etc.), which the main loop's broad except then reports as
# "errored on a message ... skipped" — a message silently never gets a
# reply/reaction, not because anything about it was wrong, just because its
# channel's own name couldn't be printed. errors="replace" is the actual
# safety net: even a character neither this nor UTF-8 can be blamed for
# becomes a "?" in the log instead of taking the message down with it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))


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


# Loaded before any project-local import below, on purpose: several of those
# modules read an env var into a module-level constant at import time —
# social_watch.POST_CHANNEL_ID, z_brain.FATHER_USER_ID, z_polls' model
# routing, feature_requests.TITLE_MODEL, and others — and a constant like
# that freezes whatever os.environ has *at that exact import*, never re-reads
# it. This used to run at the top of main(), after every one of those
# imports had already fired, which meant every such constant silently kept
# its no-env-yet default (usually "" or a hardcoded fallback) for the
# process's entire life, however long it ran. That's exactly how the first
# real `!register` went unanswered: social_watch.is_configured() checks
# POST_CHANNEL_ID, which had already frozen empty by the time .env loaded.
_load_env_file(os.path.join(_HERE, ".env"))

import activity_log
import discord_roles
import porygon_names
import z_bits
import feature_requests
import quotes
import porygon_z
import proc_guard
import social_watch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("porygon.quotes_watch_local")

_POLL_SECONDS = 10
_ICON_PATH = r"C:\Users\Williwaugh\Desktop\emotes\porygoncuter.png"
_GIT_TIMEOUT = 30

# Never let a stalled credential prompt (e.g. no saved credentials in this
# session, such as under a SYSTEM/no-login scheduled task) hang the loop.
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")


# `git rebase --continue` re-opens the commit message in an editor, and this
# runs under pythonw with no console — git's fallback editor then blocks
# forever, the rebase never finishes, and every later cycle trips over the
# leftover .git/rebase-merge. Left alone it piles up one stuck git pair per
# cycle and the working tree sits rewound to upstream the whole time, which
# looks exactly like the repo having been wiped.
_GIT_ENV = {
    **os.environ,
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    # Same hang, different prompt: a push that wants credentials.
    "GIT_TERMINAL_PROMPT": "0",
}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill git *and anything it spawned*. The process that hangs is a
    grandchild (git's own `git commit -e`), and killing only the process we
    started leaves it holding .git/rebase-merge and the index lock."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True, timeout=_GIT_TIMEOUT,
        )
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("git process survived the kill")


def _git(*args: str) -> int:
    proc = subprocess.Popen(["git", "-C", _HERE, *args], env=_GIT_ENV)
    try:
        return proc.wait(timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning(f"git {' '.join(args)} timed out — killing it and anything it spawned")
        try:
            _kill_tree(proc)
        except Exception as e:
            logger.warning(f"Failed to kill stuck git: {e}")
        return 1


def _clear_stranded_rebase() -> bool:
    """An earlier cycle that died mid-rebase leaves .git/rebase-merge behind,
    and until it's cleared the working tree stays rewound to upstream and
    every later git call fails on it. True if one was found and aborted."""
    git_dir = os.path.join(_HERE, ".git")
    if not any(os.path.isdir(os.path.join(git_dir, d)) for d in ("rebase-merge", "rebase-apply")):
        return False
    logger.warning("Found a stranded rebase from an earlier cycle — aborting it")
    activity_log.log("\U0001f47e Cleared a stranded git rebase")
    _git("rebase", "--abort")
    return True


def _merge_scan_state_conflict(rel_path: str) -> bool:
    """Auto-resolve a rebase conflict in a channel_id -> watermark scan-state
    file (quotes_scan_state.json's plain message-id strings, or
    porygon_z_state.json's {"after": id, "last_fired": ts} dicts) by keeping
    the larger (further-advanced) value per key/subkey — safe because
    discord_sync.yml runs the same scan logic on its own cadence.
    """
    def newer(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            return {k: newer(a.get(k), b.get(k)) if k in a and k in b else a.get(k, b.get(k)) for k in a.keys() | b.keys()}
        # _recent_replies is a plain list (no watermark to compare) — treat
        # the longer one as further-advanced, same spirit as the int case.
        if isinstance(a, list) and isinstance(b, list):
            return a if len(a) >= len(b) else b
        return a if int(a) >= int(b) else b

    try:
        ours = subprocess.run(
            ["git", "-C", _HERE, "show", f":2:{rel_path}"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT, env=_GIT_ENV,
        )
        theirs = subprocess.run(
            ["git", "-C", _HERE, "show", f":3:{rel_path}"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT, env=_GIT_ENV,
        )
        if ours.returncode != 0 or theirs.returncode != 0:
            return False
        merged = json.loads(ours.stdout)
        for key, value in json.loads(theirs.stdout).items():
            merged[key] = newer(merged[key], value) if key in merged else value
        with open(os.path.join(_HERE, rel_path), "w") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
        return True
    except Exception as e:
        logger.warning(f"Auto-merge of {rel_path} failed: {e}")
        return False


def _commit_and_push(paths: list[str], message: str):
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return
    # Before touching anything: if a previous cycle stranded a rebase, the
    # tree is currently rewound to upstream and staging from it would commit
    # the wrong state.
    _clear_stranded_rebase()
    if _git("add", *paths) != 0:
        return
    if subprocess.run(
        ["git", "-C", _HERE, "diff", "--cached", "--quiet", "--"] + paths,
        timeout=_GIT_TIMEOUT, env=_GIT_ENV,
    ).returncode == 0:
        return  # nothing staged
    # Scope the commit to these paths so unrelated staged work never gets
    # swept into a bot commit.
    if _git("commit", "-m", message, "--", *paths) != 0:
        return
    state_files = [p for p in ("quotes_scan_state.json", "porygon_z_state.json") if p in paths]
    for _ in range(3):
        if _git("push") == 0:
            return
        # --autostash is essential: each feature commits only its own files, so
        # the other feature's pending edits sit unstaged and would otherwise
        # abort the rebase, deadlocking every future push.
        if _git("pull", "--rebase", "--autostash") == 0:
            continue
        if (
            state_files
            and all(_merge_scan_state_conflict(p) and _git("add", p) == 0 for p in state_files)
            and _git("rebase", "--continue") == 0
        ):
            continue
        _git("rebase", "--abort")
        break
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
            if porygon_z.is_configured() and porygon_z.scan_and_process(bot_user_id):
                _commit_and_push(
                    ["porygon_z_state.json", "z_reply_history.txt"], "Update Porygon Z state [skip ci]",
                )
            if feature_requests.flush_if_dirty():
                _commit_and_push(["feature_requests.json"], "Update feature requests [skip ci]")
            if porygon_names.flush_if_dirty():
                _commit_and_push(["porygon_names.json"], "Update what Porygon calls father [skip ci]")
            if porygon_names.flush_if_dirty(subject="porygon"):
                _commit_and_push(["porygon_sibling_names.json"], "Update what Z calls Porygon [skip ci]")
            if z_bits.flush_if_dirty():
                _commit_and_push(["z_bits.json"], "Update Z's bits [skip ci]")
            # Local-only, never committed: social_profiles.json/social_pending.json/
            # social_watch_state.json are gitignored (real Discord accounts tied to
            # real social handles), and this only runs here, not in GitHub Actions,
            # so there's nothing to reconcile via git either.
            if social_watch.is_configured():
                social_watch.scan_and_process()
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
    proc_guard.kill_duplicate_instances(os.path.basename(__file__))
    # .env is already loaded — see the module-level call above, right after
    # _load_env_file is defined and before any project import.

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
