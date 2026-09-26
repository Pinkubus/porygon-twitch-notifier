"""One-off backfill: answer !z messages in the porygon channel that were
skipped because they weren't posted as a reply (pre-fix _handle_z_command
behavior). Does NOT touch porygon_z_state.json's per-channel "after" cursor —
only reply_count/recent_replies, same fields scan_and_process updates.
"""
import os
import json

def _load_env(path=".env"):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()

import discord_roles  # noqa: E402
import porygon_z  # noqa: E402
import activity_log  # noqa: E402

CHANNEL_ID = "1546322270140768316"
CHANNEL_NAME = "\u25b3\u25bdporygon\u25bd\u25b3"
MESSAGE_IDS = ["1549822778386022492", "1549947755311665235"]

token = os.environ["DISCORD_BOT_TOKEN"]
z_token = porygon_z._z_token()
bot_user_id = discord_roles.get_bot_user_id(token)
z_user_id = porygon_z._z_user_id(z_token, bot_user_id)

state = porygon_z._load_state()
reply_count = state.get("_reply_count", 0)
recent_replies = list(state.get("_recent_replies", []))

for msg_id in MESSAGE_IDS:
    msg = discord_roles.get_message(CHANNEL_ID, msg_id, token)
    if not msg:
        print(f"{msg_id}: no longer exists, skipping")
        continue
    print(f"{msg_id}: content={msg.get('content')!r} author={msg.get('author', {}).get('username')}")
    ok = porygon_z._handle_z_command(
        msg, CHANNEL_ID, CHANNEL_NAME, token, z_token, reply_count, recent_replies,
    )
    print(f"  -> {'replied' if ok else 'declined/failed'}")
    if ok:
        reply_count += 1

state["_reply_count"] = reply_count
state["_recent_replies"] = recent_replies[-porygon_z.RECENT_REPLIES_LIMIT:]
porygon_z._save_state(state)
print("state updated (reply_count/recent_replies only)")

if activity_log.flush_if_dirty():
    print("activity.log updated")
