# Porygon Twitch Notifier

Posts a Discord embed (as **Porygon**) whenever one of a configured list of
Twitch channels goes live, within ~30 seconds. Runs entirely on GitHub
Actions instead of a locally-running process, so it works regardless of
whether your own computer is on.

## How it works

GitHub Actions' `schedule` trigger can't fire more often than every 5
minutes, which isn't fast enough for near-real-time alerts. Instead:

- `loop.py` runs as a long-lived job that checks `/streams` every ~30
  seconds and posts a webhook message for any channel that just
  transitioned offline → live. It exits cleanly after ~5h55m (just under
  GitHub's 6-hour job limit).
- `.github/workflows/notify.yml` restarts `loop.py` via `schedule` every 6
  hours, leaving only a small (~5 minute) gap between runs. Runs are
  strictly serialized (`concurrency`, no overlap) since two instances
  racing to refresh the Twitch token at the same time will invalidate each
  other's token. It can also be started manually via the "Run workflow"
  button (`workflow_dispatch`), optionally with a short `max_seconds`
  override for quick test runs.
- `state.json` tracks last-known live/offline status per channel, and
  `twitch_api.py` holds the shared Twitch/Discord request logic used by
  both `loop.py` and the one-shot `notifier.py` (kept for manual testing).
- Rotated refresh tokens and state changes are committed/persisted
  immediately as they happen during the loop, not just at job end, so a
  killed or cancelled job loses as little progress as possible.
- Each loop iteration also syncs **reaction roles**: `discord_roles.py`
  polls the reactions on a designated "Pick your roles!" message (posted
  once via `setup_reaction_roles.py`) and grants/revokes the mapped role
  via the real Discord bot account whenever someone reacts/un-reacts.
  `reaction_roles.py` diffs against `reaction_state.json` to only act on
  changes. This is skipped automatically if the Discord bot vars aren't set.

## Required setup

Repo secrets (**Settings → Secrets and variables → Actions → Secrets**):

| Secret | Description |
|---|---|
| `TWITCH_CLIENT_ID` | Twitch application client ID (Public client type) |
| `TWITCH_REFRESH_TOKEN` | OAuth refresh token from the device-code flow (see below) |
| `TWITCH_STREAMS_WEBHOOK_URL` | Discord webhook URL to post notifications to |
| `GH_PAT` | A token with `repo` scope, used by the workflow to update `TWITCH_REFRESH_TOKEN` when Twitch rotates it (`gh auth token` works) |
| `DISCORD_BOT_TOKEN` | Token for the dedicated "Porygon" bot application (Developer Portal → Bot tab). Needed only for reaction roles. |

Repo variables (**Settings → Secrets and variables → Actions → Variables**), optional:

| Variable | Description |
|---|---|
| `TWITCH_CHANNELS` | Comma-separated Twitch logins to watch (defaults to `fondlyregarded,erodite,poogbooklet,onepuffman` if unset) |
| `DISCORD_GUILD_ID` | Server ID the bot manages roles in |
| `DISCORD_REACTION_CHANNEL_ID` | Channel the "Pick your roles!" message lives in |
| `DISCORD_REACTION_MESSAGE_ID` | ID of that message, printed by `setup_reaction_roles.py` |
| `REACTION_ROLE_MAP` | JSON `{"emoji": "role_id", ...}` mapping reactions to roles |

The bot's role must sit **above** any role it needs to grant/revoke in the
server's role hierarchy (Server Settings → Roles), and it needs the
**Manage Roles** permission.

## Getting/renewing the refresh token

Twitch **Public** client refresh tokens expire **30 days** after being
issued if unused. However, Twitch also rotates the refresh token on every
use — `loop.py` automatically persists the rotated token back into the
`TWITCH_REFRESH_TOKEN` secret as soon as it happens (via the `GH_PAT`
secret), so you normally shouldn't need to do this manually at all.

If the workflow does start failing (e.g. `GH_PAT` expired or was revoked,
breaking the auto-persist step), check the Actions tab — a failed run means
the refresh token most likely became invalid and needs to be renewed:

```
pip install requests
set TWITCH_CLIENT_ID=your_client_id   # (Windows) or export on macOS/Linux
python authorize.py
```

Follow the printed URL, enter the code, then copy the printed
`refresh_token` value into the `TWITCH_REFRESH_TOKEN` secret.


