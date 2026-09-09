"""
watch_activity.py — live console tail of activity.log (Twitch notifications,
quote saves/callback reactions, reaction-role grants/revokes).

`gh run view --log` refuses to return anything for a run that's still in
progress ("logs will be available when it is complete"), and `gh run watch`
only shows step-level checkmarks, not the loop's actual stdout. So instead
this polls the repo via `git pull` and prints any new lines appended to
activity.log by loop.py running in GitHub Actions — bounded only by the
~30s loop cycle + push time, not by job completion.

Run via porygon_logger.bat, or directly: python watch_activity.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(REPO_DIR, "activity.log")
POLL_SECONDS = 15


def _git_pull() -> None:
    result = subprocess.run(
        ["git", "pull", "--ff-only", "--quiet"],
        cwd=REPO_DIR, capture_output=True, text=True,
    )
    if result.returncode != 0 and result.stderr.strip():
        print(f"[git pull warning: {result.stderr.strip()[:200]}]")


def _read_lines() -> list[str]:
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def main() -> int:
    print("Watching porygon activity.log (Twitch notifications, quote saves/")
    print("reactions, reaction-role grants) \u2014 scan/plumbing noise excluded.")
    print(f"Polling every {POLL_SECONDS}s. Ctrl+C to stop.\n")

    _git_pull()
    seen = len(_read_lines())

    while True:
        try:
            time.sleep(POLL_SECONDS)
            _git_pull()
            lines = _read_lines()
            if len(lines) < seen:
                seen = 0  # file was reset/rewritten — replay from the start
            for line in lines[seen:]:
                print(line.rstrip())
            seen = len(lines)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as e:
            print(f"[watcher error: {e}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
