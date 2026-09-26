"""
proc_guard.py — kill any other already-running instance of the calling
script before a watcher/loop starts its own polling.

Two instances of the same watcher (e.g. quotes_watch_local.py started twice
by accident) both scan the same channels and can both reply to the same
message, since each has its own read/write cycle against the git-synced
state file. Call kill_duplicate_instances() once at the top of main(),
before anything else runs.
"""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger("porygon.proc_guard")


def kill_duplicate_instances(script_name: str) -> None:
    self_pid = os.getpid()
    try:
        if os.name == "nt":
            _kill_duplicates_windows(script_name, self_pid)
        else:
            _kill_duplicates_posix(script_name, self_pid)
    except Exception as e:
        logger.warning(f"Duplicate-instance check failed: {e}")


def _kill_duplicates_windows(script_name: str, self_pid: int) -> None:
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }",
        ],
        capture_output=True, text=True, timeout=15,
    )
    for line in result.stdout.splitlines():
        pid_str, _, cmdline = line.partition("|")
        pid_str = pid_str.strip()
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if pid != self_pid and script_name in cmdline:
            logger.warning(f"Killing duplicate {script_name} instance (pid {pid})")
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
                timeout=15,
            )


def _kill_duplicates_posix(script_name: str, self_pid: int) -> None:
    import signal

    result = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=15)
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmdline = int(parts[0]), parts[1]
        if pid != self_pid and script_name in cmdline:
            logger.warning(f"Killing duplicate {script_name} instance (pid {pid})")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                logger.warning(f"Failed to kill pid {pid}: {e}")
