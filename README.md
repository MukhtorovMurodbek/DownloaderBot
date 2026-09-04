# DownloaderBot

A Telegram bot that downloads media from a pasted link and sends it back. No
command is needed — pasting a supported link is the whole interface.

| platform | what it returns |
|---|---|
| Instagram | Reels, photo posts and carousels |
| TikTok | Videos without the watermark, and photo slideshows |
| Twitter / X | Video and photos; a text-only post is rendered as an image card |
| Pinterest | The pin's image at full resolution, or its video |
| Reddit | Media posts directly; text and link posts as an image card |

Runs as its own process, its own repository and its own deployment, and can
be run entirely standalone. It shares a Postgres database with four sibling
bots only in the sense that its tables live in a schema of their own inside
it (`DB_SCHEMA`); no other bot reads or writes them. The one shared area is
`family.*`, where the bot posts a heartbeat and any crash so a monitoring bot
can watch it — `FAMILY_BUS=off` disables that entirely.

---

## Where a download actually comes from

This is the part of the bot worth understanding before reading any of it.

Several of these sites decide what to serve partly from **where the request
comes from**. Every cloud host's addresses sit in published datacenter
ranges, and those ranges are served a login page where a residential
connection is served the post. A downloader that makes its requests from
inside its own container therefore works on a laptop and fails in
production, for reasons that look like bugs and are not.

So the bot does not have one way to fetch a link. Each platform has a
**chain of independent providers**, tried in order, and the chain remembers
what it learns:

- A provider that fails does not end the attempt; the next one is tried.
- Repeated failures move a provider to the back of its chain and rest it for
  a while, with the delay growing each time — rather than removing it, since
  most of these outages are temporary.
- A chain whose every member is resting still tries every member. "The site
  is slow today" and "the bot is broken" are different answers and only one
  of them is worth showing.
- Requests that go out from the container's own address are tried **last**,
  because that is the address these sites treat differently.

`/providers` prints what the chain currently believes, per platform, with
each provider's success and failure counts and its last error. `/probe`
actively tries every route against known-good sample posts and reports what
happened, which answers "is it broken, or was that one post private".

---

## Commands

| command | what it does |
|---|---|
| *(paste a link)* | Downloads it and sends it back |
| `/lossless on\|off` | Send downloads as files rather than as photos and videos. Telegram re-encodes anything sent as media — that compression is what makes it play inline, and there is no way to have both — so with this on the bytes arrive exactly as the source had them, at the cost of a tap to open. Off by default, remembered per user. |
| `/caption on\|off` | Toggle the credit caption. |
| `/cancel` | Asks which of the things the bot is waiting on should stop, one button each. |
| `/donate` | Voluntary contribution towards hosting, paid in Telegram Stars. |
| `/start` | Instructions. The first `/start` from a new user asks which language to use, once. |
| `/language`, `/en`, `/uz`, `/rus` | Switch language. Each reprints the instructions in the language chosen. |
| `/help` | The instructions on their own. |

Restricted to the account ids in `DBOT_ADMIN_ID`, and answering everyone else
exactly as a misspelt command does: `/providers`, `/probe`,
`/messageas <user_id> <text>`, `/dbdump` and `/status`.

---

## Limits

This is the bot in its family with real resource spikes — a fetch, an
`ffmpeg` mux and an upload, all at once — so it is bounded on three axes:

- **Two downloads at a time** across the whole process, which is what stops
  ten links pasted in one minute from becoming ten simultaneous downloads.
- **One of those slots per person**, so nobody can hold both and leave
  everyone else queueing behind a stranger.
- **A rolling hourly and daily allowance per person**, counted in the
  database so a redeploy does not reset it. Anyone who has contributed
  through `/donate` gets a larger allowance, permanently and for any amount.

All three are configurable; see `.env.example`. Size and duration ceilings
stop a mis-pasted link to a three-hour stream before the first byte is
fetched.

---

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in DBOT_TOKEN and DBOT_USERNAME
python bot.py
```

`DBOT_TOKEN` and `DBOT_USERNAME` come from
[@BotFather](https://t.me/BotFather). `DBOT_ADMIN_ID` is optional and takes
one or more numeric account ids.

The bot needs a Postgres database (`DATABASE_URL`, with `DB_SCHEMA`
defaulting to `downloader_bot`) and `ffmpeg` on `PATH`. The schema and its
tables are created on first run.

Two optional settings materially change how well Instagram works from a
datacenter: `DBOT_IG_COOKIES_FILE` and `DBOT_IG_PROXY`. Both are documented
in `.env.example`.

### Deploying

Set the same values as environment variables on the host and run
`python bot.py`. `railway.json` and `nixpacks.toml` configure a Railway
deployment; neither is required elsewhere.

A deployment replaces the running container, and the bot is built so that
costs as little as possible. The new process waits on a Postgres advisory
lock until the old one has stopped polling, so Telegram never sees two
consumers of one token. What each provider had learned is written to the
database, so a restart does not go back to hammering a route that has been
dead for a week. A download that was running when the signal arrived cannot
survive — the container is going — so the person is told, rather than left
watching a message that will never finish. Updates sent during the gap are
held by Telegram and delivered on the first poll. `DEPLOY_SAFETY=off`
disables the deploy machinery.

### Keeping two databases in sync

`db_merge.py` reconciles a local database with a remote one additively — it
never deletes or overwrites.

```bash
python db_merge.py --from local --into cloud --dry-run
python db_merge.py --from local --into cloud
```

---

## Files

| file | |
|---|---|
| `bot.py` | Handlers, the send path, and the per-person limits |
| `resolvers.py` | The provider chains, their health, and the probe |
| `platforms.py` | Which links are recognised, and the platform-specific fetches |
| `net.py`, `video.py` | Bounded HTTP, and the video download |
| `cards.py` | Rendering a text post as an image |
| `db.py` | This bot's schema, provider health, allowances, connection pool |
| `i18n.py` | English, Uzbek and Russian strings |
| `family_link.py` | Heartbeats, crash reporting, and the command queue a monitoring bot uses |
| `lifecycle.py` | Surviving a redeploy: one poller at a time, state in Postgres |
| `live_message.py` | When a bot message may keep evolving in place |
| `shared_features.py` | Donations, logging, activity tracking, flood control |
| `db_merge.py` | Reconciles two databases, additively |

`family_link.py`, `lifecycle.py`, `live_message.py` and `shared_features.py`
are shared with the sibling bots by being copied rather than imported: each
bot is a separate deployment, so nothing crosses a repository boundary.

## Requirements

Python 3.11 or newer, Postgres 16 or newer, and `ffmpeg`.

## A note on scope

This bot downloads publicly accessible posts on behalf of the person asking
for them. It is not a way around a private account, and it does not attempt
to be. Whoever runs it is responsible for how it is used where they run it.
