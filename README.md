# Porygon Twitch Notifier

Posts a Discord embed (as **Porygon**) whenever one of a configured list of
Twitch channels goes live. Runs on a GitHub Actions schedule (every 5
minutes) instead of a locally-running process, so it works regardless of
whether your own computer is on.

## How it works

- `notifier.py` runs once per workflow trigger: refreshes a Twitch access
  token, checks `/streams` for the configured channels, and posts a webhook
  message for any channel that just transitioned offline → live.
- `state.json` tracks last-known live/offline status per channel and is
  committed back to the repo by the workflow after each run.
- `.github/workflows/notify.yml` triggers the check every 5 minutes via
  `schedule`, and can also be run manually via the "Run workflow" button
  (`workflow_dispatch`).

## Required setup

Repo secrets (**Settings → Secrets and variables → Actions → Secrets**):

| Secret | Description |
|---|---|
| `TWITCH_CLIENT_ID` | Twitch application client ID (Public client type) |
| `TWITCH_REFRESH_TOKEN` | OAuth refresh token from the device-code flow (see below) |
| `TWITCH_STREAMS_WEBHOOK_URL` | Discord webhook URL to post notifications to |
| `GH_PAT` | A token with `repo` scope, used by the workflow to update `TWITCH_REFRESH_TOKEN` when Twitch rotates it (`gh auth token` works) |

Repo variable (**Settings → Secrets and variables → Actions → Variables**), optional:

| Variable | Description |
|---|---|
| `TWITCH_CHANNELS` | Comma-separated Twitch logins to watch (defaults to `fondlyregarded,erodite,poogbooklet` if unset) |

## Getting/renewing the refresh token

Twitch **Public** client refresh tokens expire **30 days** after being
issued if unused. However, Twitch also rotates the refresh token on every
use — the workflow automatically persists the rotated token back into the
`TWITCH_REFRESH_TOKEN` secret after each run (via the `GH_PAT` secret), so
you normally shouldn't need to do this manually at all.

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

