import os
import discord_roles

token = os.environ["DISCORD_BOT_TOKEN"]
channel_id = os.environ["DISCORD_REACTION_CHANNEL_ID"]

for emoji in ["\u2122\uFE0F", "\u2122"]:
    ok = discord_roles.add_own_reaction(channel_id, "1547092553814249543", emoji, token)
    print(f"Tried {emoji!r} ({[hex(ord(c)) for c in emoji]}): {ok}")
