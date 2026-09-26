"""One-off, read-only: find the 'porygon' channel and list any still-present
!z command messages in it (i.e. ones _handle_z_command never replied to and
therefore never deleted). Posts nothing."""
import os

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

token = os.environ["DISCORD_BOT_TOKEN"]
guild_id = os.environ["DISCORD_GUILD_ID"]

channels = discord_roles.get_guild_text_channels(guild_id, token)
print(f"{len(channels)} text channels:")
for c in channels:
    print(f"  {c['id']}  {c.get('name')!r}")

matches = [c for c in channels if "porygon" in (c.get("name") or "").lower()]
print(f"\nChannels matching 'porygon': {[(c['id'], c.get('name')) for c in matches]}")

for ch in matches:
    channel_id = ch["id"]
    print(f"\n--- scanning #{ch.get('name')} ({channel_id}) for un-replied !z messages ---")
    before = None
    total = 0
    hits = []
    while True:
        batch = discord_roles.get_channel_messages(channel_id, token, before=before, limit=100)
        if not batch:
            break
        total += len(batch)
        for m in batch:
            content = (m.get("content") or "").strip()
            words = content.lower().split()
            if words and words[0] == "!z":
                hits.append(m)
        before = min(batch, key=lambda m: int(m["id"]))["id"]
        if len(batch) < 100:
            break
    print(f"  scanned {total} messages total; found {len(hits)} un-replied '!z' messages")
    for m in sorted(hits, key=lambda m: int(m["id"])):
        author = m.get("author", {})
        name = author.get("global_name") or author.get("username")
        ref = (m.get("message_reference") or {}).get("message_id")
        print(f"    id={m['id']} by={name} replying_to={ref} content={content!r}" if False else
              f"    id={m['id']} by={name} replying_to={ref} content={m.get('content')!r}")
