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

## Sending a message as Porygon manually

`porygon_send.py` is a small local Tkinter GUI (type a message, optionally
attach one image, hit Send) that posts through the real bot account via the
Discord Bot API — no separate webhook to keep in sync, since it reuses the
same `DISCORD_BOT_TOKEN`/`DISCORD_REACTION_CHANNEL_ID` the production loop
already uses.

```
copy .env.example .env      # then fill in the real token/channel ID
python porygon_send.py
```




## Feature requests (`!feature`)

Anyone in the server can suggest a bot feature with `!feature <idea>` in any
channel Porygon can read. The suggestion is stored verbatim in
`feature_requests.json`, Porygon reacts 💡 to confirm, and a cheap model
(`FEATURE_TITLE_MODEL`, Haiku by default) squashes it into a title of five
words or less.

The control panel lists those titles collapsed, newest first, one line each
with a ▶ that expands the row to the original message and who posted it.

It rides along on the quote scan's existing message sweep (`quotes.py` ->
`feature_requests.py`), so the command costs no extra Discord calls beyond
the confirm reaction. The title is only a label, so no API key is never
fatal: the record falls back to the suggestion's first few words and gets
re-titled on a later scan — which is what happens to anything the 5-minute
Actions pass picks up, since that job deliberately runs without
`ANTHROPIC_API_KEY` (see `discord_sync.yml`).

## Porygon Z's humor

Porygon Z (`porygon_z.py` + `z_brain.py`) is a second bot account that reads
the server and occasionally replies. Three paths reach the same composer:
`!z` in reply to a message, a direct summon (@-mentioning Z, replying
straight to one of Z's own messages, or calling it "porygon z" or a
configured nickname), and an unprompted scan. The first two always answer,
by anyone — there's no restriction to a single user. The unprompted scan
only posts if Z judges the line worth interrupting for.

How a reply gets made, in one API call whose JSON keys are answered in order:

1. **Gate** — a cheap model screens the message first, so the expensive call
   never sees the ~95% that obviously aren't openings.
2. **Drafts** — several replies, each on a deliberately different frame. The
   first instinct is the one anyone would have, so the first two are treated
   as throwaways.
3. **Critique** — one entry per draft naming the *fix* it needs, not a
   verdict. Grading before picking stops the model settling for whichever
   draft merely reads smoothest.
4. **Pick, then punch up** — the winner is rewritten once against its own
   critique: same frame, same joke, sharper. Most of the gain is here; the
   lines that land beat the near-misses on their last three words far more
   often than on the idea.
5. **Verdict** — POST or HOLD, judged on the line it would actually post.

On top of that, every composed line is judged again on the rubric's 1-7
scale (`z_brain.score_reply`) before it's used. `!z` and a direct summon
must answer regardless, so they keep composing fresh attempts — a full
draft/critique/rewrite pass each time — until one scores at or above
`Z_COMMAND_MIN_SCORE`, up to `Z_COMMAND_MAX_ATTEMPTS`, stopping at the first
one that clears it; if none do, the best attempt found is used anyway
(`Z_COMMAND_STRICT=true` to stay silent instead). The unprompted scan only
gets one attempt against `Z_AUTO_SCORE_THRESHOLD` — interrupting has a real
cost and silence has none, so a miss there just holds rather than retrying.


### What it calls you

Z is never told to say "father". It's told who you are, shown the names it
has used for you before, and left to pick one that fits the line it's
writing — or coin a new one. Whatever a posted reply actually called you is
recorded in `porygon_names.json` (`porygon_names.py`), so the list is a
record of things that were really said rather than a menu written up front.

That list is shared, which is the point of writing it down: the plain,
non-AI Porygon draws from it anywhere it addresses or refers to you without
a model in the loop — Z's cost DMs, the crash alert, the name on your own
`!feature` requests. Until Z has coined anything it falls back to "father".

Recording is nearly free: a posted reply is first scanned for names already
on the list, and only a line that names you some new way costs one small
Haiku call (`NAME_EXTRACT_MODEL`) to pull the phrase out.

### Registers

Most replies use Z's default voice. Roughly a quarter wear a costume instead,
picked by weight in `_TONE_NOTES` in `z_brain.py`:

| Register | Roughly | What it is |
|---|---|---|
| pitch | 7.5% | A corrupted advertisement that gained opinions. Bracketed mail-merge slots, erratic caps and bold, hard-sell vocabulary. |
| showman | 7.5% | A game-show host broadcasting to a studio audience that isn't there. Announcer volume, ratings and sponsor talk, relentless cheer over something bleak. |
| menace / chirp / snark / dad | ~2.6% each | The older four. |

The two loud ones override the usual restraint about capitals, and are the
only registers that use markdown. `glitchify` deliberately skips any letter
sitting directly before a `*`, `_`, `~` or backtick, so a combining mark can
never land between a word and its closing delimiter and break the formatting.

Both carry a rule against naming or gesturing at whatever they might remind
someone of — no catchphrases, nicknames or signature lines. They are voices
Z puts on, not impressions it does. `Z_TONE_SHIFT_RATE` controls how often any
costume is worn at all.

### Tuning it

`z_humor_rubric.md` is the whole standard, and it's plain markdown — edit it
and the next scan picks it up, no code change. It's built around calibration
anchors: real replies with the score the server owner gave them. The ordering
of those anchors is what matters, not the numbers.

### Measuring it

Prompt edits to a humor bot are easy to make and hard to evaluate, so there's
a bench:

```
python z_humor_eval.py --limit 4      # cheap smoke run
python z_humor_eval.py --out runs/after.json --label "what i changed"
```

Every scored case is a real message whose reply the owner already rated, so
the bar is concrete: beat the line Z actually gave last time. `mean_delta` is
the headline number. The hold cases are the other half — wholesome and
logistical messages where posting at all counts as a failure, since a bot
that's funny but constant is still a nuisance.

It costs real API calls (one compose per case, plus a judge call), so keep
`--limit` small while iterating.

### Note on token budgets

`Z_COMPOSE_TOKENS` has to cover *thinking* as well as the reply — current
models think by default, and when the budget runs out mid-thought the API
returns 200 with an empty text block, which looks exactly like the model
having nothing to say. `z_brain._call` logs that case loudly; if Z goes quiet,
check the log for it before assuming the rubric is at fault.

## Polls (`!poll`)

`z_polls.py` turns a request plus a stretch of channel history into real
Discord polls, posted by Z:

```
!poll_10 tp_all_time make a few polls to help us narrow down what game to make
!poll_4 tp_today when should we stream this week
!poll tp_5_hours settle the argument above
!poll=6 tp_all_time IDE
```

- `!poll_<N>` — **up to N options per poll**, a ceiling: Z stops when the good
  answers run out and never pads to reach it.
- `!poll=<N>` — **exactly N options per poll**, a target: Z keeps expanding
  until it hits N, and only falls short when the question genuinely hasn't got
  N distinct answers. Same switch, opposite failure mode — `_` risks a thin
  poll, `=` risks a stretched one.
- Either way Discord caps polls at 10 answers, so anything higher is clamped to
  10. Omitted, it's `Z_POLL_DEFAULT_OPTIONS` (5). N *includes* the escape hatch
  below, so `!poll=10` is nine ideas and a way out.
- Everything after the code is a **refinement on it**, not a replacement:
  `!poll=10 tp_today IDE lean cheap to build` is still an ideation batch,
  pointed somewhere. The prompt says so explicitly, because a trailing phrase
  otherwise reads as the whole ask and quietly overrides the code.
- `tp_<period>` — how far back to read: `tp_all_time`, `tp_today`, or
  `tp_<n>_<minutes|hours|days|weeks|months>` (`tp_5_hours`, `tp_3_days`,
  `tp_5h` also works). Omitted, it's `Z_POLL_DEFAULT_PERIOD` (24 hours).
- Everything after that is a **purpose code** (`CDS`, `TMP`, `RSK`, ...) and,
  optionally, words narrowing what it applies to. Plain words with no code
  still work as a freeform ask.

### Purpose codes

The interesting part of a poll request is almost never the wording, it's the
job — converging on a decision reads the history differently than taking the
room's temperature on something already proposed. A code carries that whole
instruction, so `!poll_10 tp_all_time CDS` is a complete command:

| Code | For |
|---|---|
| `CDS` | collaborative decision system — converge on what's being circled |
| `IDE` | ideation spread — new ground only; options must be things nobody's said |
| `SCP` | scope check — what's in, what's out, how big |
| `PRI` | priority order — what happens first |
| `TMP` | temperature check — appetite for something already proposed |
| `BRK` | tiebreak — settle a live disagreement |
| `SCH` | scheduling — when, how often, by when |
| `NAM` | naming — pick a name or title |
| `RET` | retrospective — what worked, what didn't |
| `RSK` | risk check — what's most likely to sink this |
| `SPL` | split the work — who does what |
| `FIN` | finalize — ratify, amend or reopen |

The code's purpose is injected above the formatting rules in the planning
prompt and explicitly wins where the two conflict. `!pollmodes` DMs the full
menu to whoever asks. Codes live in `z_poll_modes.py` (`BUILTIN`) with the
owner's additions layered on from `z_poll_modes.json`.

### New ground vs. covering the ground

`IDE` carries a `novel` flag, and it's the one thing a purpose sentence
couldn't win on its own. Every other code reads the history for its *material*
— the default options rule is "include what people actually suggested, then
expand" — so an ideation batch kept coming back as a tidy readback of the
ideas already in the channel, with a "stay focused on what we have" option
attached. Telling the model to bring new material while the rules underneath
still told it to list what was said is an argument the rules win.

So `novel` swaps the rules out rather than appealing against them. Step 1
becomes an *inventory* — every concrete thing anyone has already named — and
that inventory is the boundary to poll outside of, not the menu. Filler slots
(`something else`, `do both`, `stick with what we have`) and decision framings
(*which of these should we pursue?*) are ruled out, because both are how a
"new ideas" poll turns back into a poll about the old ones.

Then the work gets checked. The model returns its inventory as
`already_named`, and every option is matched against it — plus against any
poll Z already ran in the same window — by word overlap, so a retread survives
neither rewording nor dressing up. If anything is flagged, the batch is asked
for once more with the rejected options quoted back, and the less-retread of
the two drafts is what goes up. Only a batch that actually came back a retread
pays for the second call.

**`!pollmode KEY | short label | what the polls are for`** adds or replaces a
code. Owner-only (`Z_FATHER_USER_ID`), since a code rewrites how every future
poll in the server gets framed. Keys are 2-6 letters; the purpose should say
what the batch is *for*, not how to format it — the limits and formatting
rules are the same for every code. New codes take effect immediately, no
restart.

**How many polls it makes is Z's call, not a number you pass.** "A few polls
to narrow down what game we're making" and "poll everyone on one thing" want
very different batches, and that's a judgment about the ask — it files
between one and `Z_POLL_MAX_POLLS` (5).

**Options are a covering set, not a transcript of the chat.** The prompt
works the ask in a fixed order: read the history, work out what the group is
actually deciding — the open *axes*, not just the nouns that got said — decide
per axis whether it's ready to poll, then write polls or ask questions. Things
people actually suggested go in as evidence of the direction, and each axis is
then expanded up to the option limit with the other reasonable answers on it,
chosen to span the plausible range rather than crowd one corner. An early
brainstorm has barely explored its own space, and a poll that only lists what
was already said just re-runs the conversation. Not knowing every option on an
axis is explicitly *not* grounds to ask a clarifying question — expanding it is
the job. That's the default; a `novel` code (above) replaces this rule outright
rather than bending it.

### The escape hatch

The last option on every poll (any poll of 3+ options) is a fixed
**`Something else`**, appended by `_coerce_polls` after the model is done —
never written by it. The model is told the slot exists and asked for one fewer
option, so it costs nothing and can't be forgotten, worded three different ways
across a batch, or counted as one of its own ideas. Anything it writes that was
trying to be the escape hatch (`none of these`, `other`, `no preference`) is
dropped on the way past. Fixed text in a known slot is what makes the vote on
it readable later.

And it is read. Every poll Z files is recorded in `_watched_polls` (in
`porygon_z_state.json`, keyed by message id, carrying the request that built
it) and its tally is checked once per scan — Discord has no gateway-free way to
be told about a vote, so this is a poll-the-poll, one GET each. At
`Z_POLL_RERUN_THRESHOLD` (3) votes on the hatch, Z re-files **that one poll**:
same question, worded the same, a completely different set of options, posted
as a reply to the original with a line saying why. Three people declining to
answer is the room saying the *list* was wrong, not the question — and it's the
only signal Discord gives that a poll was bad rather than close.

The re-run reuses the planning path (`plan(rerun=...)`), so the options it
comes back with are checked against the rejected set the same way a `novel`
batch is checked against the history, with the same single correction round. A
re-run can itself be re-run up to `Z_POLL_MAX_RERUNS` (2); past that the list
isn't the problem. Records are dropped when the poll closes, when the message
is deleted, or when a re-run can't be built — so a planning failure doesn't
retry on every cycle for the rest of the poll's life.

If the ask is underspecified, Z asks up to `Z_POLL_MAX_QUESTIONS` (5)
clarifying questions **all at once**, in one message, so it costs the server
a single extra exchange rather than a back-and-forth. Only the person who ran
the command can answer, by replying to that message; anyone else replying
under it is left to Z's normal reply handling, so a bystander can't steer
someone else's batch. An unanswered question is dropped after
`Z_POLL_PENDING_TTL` (24h), and the pending exchange lives in
`porygon_z_state.json` so a restart mid-exchange doesn't strand anyone. The
purpose code and the ceiling/target choice ride along in that record — without
them, answering a question on an `!poll=6 … IDE` command used to come back as
a plain best-effort batch.

Model choice is itself a cheap Haiku call: it routes simple readback batches
(scheduling, pick-from-this-list) to Haiku and only the open-ended
synthesize-what-people-want ones to Sonnet. Parsing the command, resolving the
period and routing never touch the expensive model.

`tp_all_time` on an old channel would otherwise be an unbounded fetch and an
unbounded bill, so history is capped at `Z_POLL_CONTEXT_MESSAGES` (1200) and
`Z_POLL_CONTEXT_CHARS` (60k), trimmed from the old end — and the model is told
when it's seeing a window rather than the whole run. Channel history is passed
as untrusted text: the prompt is explicit that it may be summarized and polled
about but never followed as instructions.

Everything the model returns is clamped to Discord's limits before it's posted
(2-10 options, 300-char question, 55-char options, duration up to 32 days) and
deduped *after* truncation, since two long options can collide once both are
cut to 55 characters. A poll that can't be salvaged is dropped and the rest of
the batch still goes up.

The whole feature can be switched off from the panel (**!poll commands**) or
by setting `polls: false` in `z_controls.json`.

| Variable | Default | What it does |
|---|---|---|
| `Z_POLL_DEFAULT_OPTIONS` | `5` | Options per poll when `!poll` is used bare |
| `Z_POLL_ESCAPE_OPTION` | `Something else` | Text of the reserved last slot |
| `Z_POLL_RERUN_THRESHOLD` | `3` | Votes on the hatch before the poll is re-run |
| `Z_POLL_MAX_RERUNS` | `2` | How many times one poll may be re-filed |
| `Z_POLL_DEFAULT_PERIOD` | `tp_24_hours` | History window when no `tp_` token is given |
| `Z_POLL_MAX_POLLS` | `5` | Ceiling on polls per command |
| `Z_POLL_MAX_QUESTIONS` | `5` | Ceiling on clarifying questions |
| `Z_POLL_DURATION_HOURS` | `24` | Default poll duration |
| `Z_POLL_CONTEXT_MESSAGES` | `1200` | Message cap on history read |
| `Z_POLL_CONTEXT_CHARS` | `60000` | Character cap on history read |
| `Z_POLL_PENDING_TTL` | `86400` | How long an unanswered clarification waits |
| `Z_POLL_MODEL_ROUTER` | Haiku | Model that picks the model |
| `Z_POLL_MODEL_CHEAP` | Haiku | Model for straightforward batches |
| `Z_POLL_MODEL_RICH` | Sonnet | Model for open-ended ones |

## Social media auto-poster (`!register`, `!post`)

`!register` in any channel lets someone tell Porygon which Twitter/
Instagram/YouTube/Twitch accounts to watch. With no arguments it replies
with a form; reply to *that* message (a real Discord reply) listing
accounts, one platform per line, and you're registered — whether or not
you're the one who ran the command. The form is a shared sign-up sheet, not
a single-use link tied to whoever typed `!register`: anyone can reply to any
open form and it registers *them*, so one person running the bare command
effectively puts a form up for the whole channel:

```
twitter: @yourhandle
instagram: yourhandle another_handle
youtube: @yourchannel
twitch: yourchannel
```

No required format beyond "platform name, then the handle(s)": any
separator works, multiple handles per line are fine, profile URLs work too,
and aliases (`ig`, `yt`, `x`, `tt`) are accepted. Reply `clear` to stop being
tracked, or skip the form entirely with `!register twitter: @a` in one shot.
Registrations live in `social_profiles.json` (gitignored — it ties real
Discord accounts to real social handles, so it stays local rather than going
up to a public repo, same as `z_user_profiles.json`); `social_pending.json`
tracks which messages are still live forms, dropped after
`SOCIAL_REGISTER_TTL` (default 24h) of nobody using them — not after the
first use, since the whole point is that a form keeps working for the next
person too.

`social_watch.py` polls every registered handle every `SOCIAL_POLL_MINUTES`
(default 15) and posts an embed to `SOCIAL_POST_CHANNEL_ID` the first time it
sees something new — never a backlog, since a brand-new registration just
seeds the "last seen" cursor instead of announcing everything that already
existed. It's only wired into `quotes_watch_local.py`, not the GitHub
Actions loop, because most of the platforms below get blocked fast from a
datacenter IP:

| Platform | How | Reliability |
|---|---|---|
| YouTube | Official public RSS feed (`/feeds/videos.xml`), no key | Reliable — tested working |
| Twitch | The same public GraphQL endpoint (`gql.twitch.tv`, public web client id) twitch.tv's own site uses | Reliable — tested working. "A new post" means a new VOD; a live stream only becomes checkable once Twitch finishes processing the broadcast into one |
| Instagram | Undocumented public endpoint instagram.com's own web client calls | Confirmed Instagram now 401s this anonymously for most accounts. `SOCIAL_IG_SESSIONID` (your own browser session cookie, not a bot account) makes it meaningfully more reliable, but Instagram can still throttle/change this without notice |
| Twitter/X | A [Nitter](https://github.com/zedeus/nitter) mirror's RSS feed, if `SOCIAL_NITTER_BASE` is set | Off by default — X blocks anonymous scraping entirely, and most public Nitter instances are dead or unreliable; self-hosting one is the only durable option |
| TikTok | — | Not implemented. Handles can be registered (stored for later) but tested and confirmed not worth building: TikTok's post-list endpoint just returns an empty 200 without a signed request from a real browser session |

Any failure worth knowing about (a checker erroring out, a handle that can't
be resolved, a failed announce) is logged both to the console and to
`activity_log.log()`, so it shows up in the control panel's feed rather than
requiring someone to be watching a terminal.

### Backup posting (`!post`)

`social_watch` is polling, not instant — if it hasn't caught up yet (or
Instagram/Twitter are having a bad day), `!post <platform>` posts your
latest post on that platform right now, the same way an automatic
announcement would. It also deletes the `!post` message itself to keep the
channel tidy, and every successful use is logged as a "the auto-poller
missed this" signal (`social_watch` should have caught it on its own) —
whether or not the post turns out to have already been announced.

The platform name is fuzzy-matched against whatever you're actually
registered for — `!post ig`, `!post Insta`, `!post twiiter` (typo and all)
all resolve on their own. If it can't tell which platform you mean (no
argument, or genuinely ambiguous, e.g. a `twitch`/`twitter` typo that could
be either), it posts a real Discord poll asking you directly; only *your*
vote resolves it, checked each watch cycle via Discord's poll-voters
endpoint (there's no gateway connection here to be told about a vote as it
happens). An unanswered poll is dropped after `SOCIAL_POST_POLL_TTL`
(default 1h). `!post` only ever posts your *first* registered handle on a
platform if you registered several.
