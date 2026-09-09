import os
import discord_roles

token = os.environ["DISCORD_BOT_TOKEN"]
channel_id = os.environ["DISCORD_REACTION_CHANNEL_ID"]

msgs = discord_roles.get_channel_messages(channel_id, token, after="1547092000000000000", limit=100)
for m in sorted(msgs, key=lambda m: int(m["id"])):
    reactions = m.get("reactions", [])
    react_str = ",".join(f"{r['emoji']['name']}x{r['count']}" for r in reactions)
    print(f"{m['id']} | {m.get('author',{}).get('id')} | {m.get('content')!r} | reactions=[{react_str}]")
