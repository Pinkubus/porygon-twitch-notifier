"""
discord_sync_once.py — single-pass reaction-roles sync + quotes scan, for a
short 5-minute cron (the platform's finest schedule granularity), decoupled
from the long-running Twitch loop in loop.py.

loop.py's continuous polling only restarts every ~6h, so a stuck/crashed run
(e.g. an invalid Twitch refresh token) can silently stall quotes/reaction-role
scanning for hours. This script does exactly one Discord-side pass and exits,
so it keeps working on its own 5-minute cadence regardless of the Twitch
loop's health.
"""
from __future__ import annotations

import os
import sys
import logging

import activity_log
import discord_roles
import reaction_roles
import quotes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("porygon.discord_sync_once")


def main() -> int:
    reaction_roles_enabled = reaction_roles.is_configured()
    quotes_enabled = quotes.is_configured()
    if not (reaction_roles_enabled or quotes_enabled):
        logger.info("Neither reaction-roles nor quotes are configured — nothing to do")
        return 0

    bot_user_id = discord_roles.get_bot_user_id(os.environ["DISCORD_BOT_TOKEN"])
    if bot_user_id is None:
        logger.error("Failed to resolve bot user id — check DISCORD_BOT_TOKEN")
        return 1

    if reaction_roles_enabled:
        reaction_roles.sync(bot_user_id)
    if quotes_enabled:
        quotes.scan_and_process(bot_user_id)
    activity_log.flush_if_dirty()
    return 0


if __name__ == "__main__":
    sys.exit(main())
