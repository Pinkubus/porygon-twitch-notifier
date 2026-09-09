import os
import discord_roles

token = os.environ["DISCORD_BOT_TOKEN"]
channel_id = os.environ["DISCORD_REACTION_CHANNEL_ID"]

ok = discord_roles.add_own_reaction(channel_id, "1547092553814249543", "\u2122\uFE0F", token)
print(f"Backfilled reaction: {ok}")
