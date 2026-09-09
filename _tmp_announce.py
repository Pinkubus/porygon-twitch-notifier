import os
import discord_roles

token = os.environ["DISCORD_BOT_TOKEN"]
channel_id = os.environ["DISCORD_REACTION_CHANNEL_ID"]

embed = {
    "title": "\U0001F4DD New feature: quote saving!",
    "description": (
        "- `!addquote <text>` to save something iconic someone said\n"
        "- Reacts \U0001F4DD to confirm it saved\n"
        "- Bot reacts \u2122\uFE0F if later chat repeats a saved quote\n"
        "- Works in any channel, no need to pick one"
    ),
}
message_id = discord_roles.post_message(channel_id, token, embed)
print(f"Posted message {message_id}")
