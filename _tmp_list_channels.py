import os
import discord_roles

token = os.environ["DISCORD_BOT_TOKEN"]
guild_id = os.environ["DISCORD_GUILD_ID"]
for c in discord_roles.get_guild_text_channels(guild_id, token):
    print(c["id"], c.get("name"))
