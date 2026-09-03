# DownloaderBot

Telegram bot that downloads media from a pasted link and sends it back — no
command needed, just paste a link.

- **Instagram**: reels, photo posts and carousels.
- **TikTok**: videos without the watermark, and photo slideshows.
- **Twitter/X**: video and photos; a text-only tweet becomes a clean image
  "card" instead of a wall of text.
- **Pinterest**: the pin's image at full resolution, or the video for a
  video pin.
- **Reddit**: media posts download directly; text/link posts get a card.

## How it gets them, and why that took a rewrite

Instagram and YouTube decide what to serve partly from **where** the request
comes from. Every cloud host's address sits in a published datacenter range,
and those ranges get a login page where a phone gets the post. Until v1.2.1
this bot had exactly one way to fetch anything — yt-dlp, from inside its own
container — so Instagram stopped working entirely and said so politely.

`resolvers.py` fixes that by not making the request from here. Each platform
has a **chain of independent routes**, mostly public services that fetch on
their own infrastructure and hand back either a direct CDN link or a copy
proxied through themselves; whether this container is on a blocked range
stops mattering. yt-dlp is still in every chain, but last, because it is the
one route that goes out from this address — and the only one that gets better
the moment a cookie file or proxy is configured.

Any one of those services is mortal: they get bought, rate-limited, sued, or
simply stop resolving in DNS. So the bot keeps score. Every attempt is
recorded per route in its own `provider_health` table, a route that keeps
failing is moved to the back of its chain and retried more and more rarely
(never dropped — a chain where everything is resting still tries everything),
and a single success puts it straight back at the front. Nothing needs a
human to notice a service has died.

- `/providers` (owner-only) — what the live bot has learned from real traffic.
- **`/run downloaderbot probe`** (through ParentBot) — asks every route *from
  inside the deployed container*. This is the one that settles arguments:
  every route was verified on a laptop, and a laptop is on a residential
  address, which is the exact thing these sites treat differently. Add
  platform names (`probe instagram tiktok`) or paste real links to narrow it.
- `python probe.py` — the same checks from a terminal, wherever you run it.

Both probes ask every route rather than stopping at the first that works, and
neither touches the health scores: a probe is a question, not traffic.

Read `resolvers.py`'s docstring before changing any of it. `platforms.py` is
now just link recognition plus the two page reads that feed the cards, and
`cards.py` is the card renderer.

This bot is its own process, its own repo and its own deployment — it can
be run entirely on its own. It shares one Postgres database with the rest
of the family, but only in the sense that its tables live in their own
schema inside it (`DB_SCHEMA` in `.env`); no other bot reads or writes
them. The exception is `family.*`, where this bot posts a heartbeat and
any crash so that ParentBot can watch it — see `family_link.py`, and
ARCHITECTURE.md in the family monorepo for why it is arranged this way. Set `FAMILY_BUS=off`
to opt out of that entirely.

## Commands

- (paste a supported link any time) — downloads and sends it back
- `/caption on|off` — toggle the "via @thisbot" credit caption
- `/lossless on|off` — send downloads as files instead of as photos and
  videos. Telegram re-encodes anything sent as media — that compression is
  what makes it play inline, and there is no way to have both — so with
  this on the bytes arrive exactly as the source had them, at the cost of a
  tap to open and no preview in the chat. Off by default, remembered per
  user. Bare `/lossless` shows the current state with buttons
- `/donate` — chip in for hosting costs (voluntary, Telegram Stars)
- `/cancel` — asks which of the things it is waiting on you for to stop,
  as one button each, and stops nothing until you pick. With nothing pending
  it says so straight away, as it always did
- `/start` — the full instructions. The first `/start` from a brand-new
  user asks for a language before printing them, which is the one and only
  time it asks; after that it prints them in the language on record
- `/language` — the picker on demand: a short greeting in all three
  languages and one row of buttons, with a tick on the language in force.
  Choosing one (even the one already set) reprints the instructions in it
- `/en`, `/uz`, `/rus` — switch language directly, skipping the picker;
  each also reprints the instructions in the language just chosen
- `/help` — the instructions on their own

Owner-only (requires `DBOT_ADMIN_ID` in `.env` — silently do nothing for
everyone else):
- `/messageas <user_id> <text>` — send a message to that user as this bot
- `/dbdump` — export this bot's tables as a zip of CSVs
- `/status` — uptime, host, crashes since this process started, active users
- `/providers` — which download route is currently working, per platform,
  with the failure streak and last error for the ones that are not

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in `DBOT_TOKEN`/`DBOT_USERNAME`
   (from [@BotFather](https://t.me/BotFather)). Optionally set `DBOT_ADMIN_ID`
   to your own numeric user id(s) to unlock `/dbdump`.
3. Start the family's shared Postgres, from the monorepo root:
   ```
   docker compose up -d
   ```
   That is one database (`botfamily`) for all five bots, with a schema
   each — this one uses `DB_SCHEMA=downloader_bot`. No Docker? Install
   Postgres directly and point `DATABASE_URL` at it instead.
4. Run it:
   ```
   python bot.py
   ```

## Deploying (e.g. Railway)

The short version is below; DEPLOY.md in the family monorepo covers all five in one
pass, which is easier than doing five of these separately.

1. Point `DATABASE_URL` at the family's Postgres, and set `DB_SCHEMA` to
   `downloader_bot`. On Railway that first one is a reference variable,
   `${{Postgres.DATABASE_URL}}`, so several services can share one database.
2. Set `DBOT_TOKEN`, `DBOT_USERNAME`, and the rest of `.env` as environment
   variables on the service.
3. Deploy — `pip install -r requirements.txt` then `python bot.py`.

### Updating a running bot

Pushing an update replaces the container, and the bots are set up so nobody
notices: the new process waits on a Postgres advisory lock until the old one
has stopped polling (no 409 Conflict, no split updates), open conversations
and half-finished sessions are restored from the `runtime_state` table in
this bot's own schema, and anyone whose upload was mid-flight is told to send
it again rather than left waiting. Updates sent during the gap are held by
Telegram and delivered on the first poll.

`../UPDATES.md` has the whole picture, including what to check after a push.
`DEPLOY_SAFETY=off` turns all of it off and restores the old behaviour.

## Keeping a local and cloud database in sync

If you ever run this bot from both your laptop and the cloud at different
times, `db_merge.py` reconciles the two additively (never deletes or
overwrites anything):
```
python db_merge.py --from local --into cloud --dry-run   # preview first
python db_merge.py --from local --into cloud             # actually do it
```
Read the script's docstring for exactly how conflicts are handled.

## Optional: cross-promoting sibling bots

If you're running this alongside other bots (e.g. a sticker or converter
bot) and want each to mention the others in `/start`/`/help`, set
`SIBLING_BOTS` in `.env` — see the comment in `shared_features.py`. Purely
cosmetic (display text + link buttons); no database or file is shared.

## Files

- `bot.py` — handlers and the per-platform routing
- `platforms.py` — link recognition + Pinterest/Reddit/Twitter fetch logic
- `cards.py` — the Reddit/Twitter text-post "card" image renderer (Pillow)
- `db.py` — this bot's own Postgres schema and queries
- `family_link.py` — heartbeats, crash reporting, and the queue ParentBot
  uses to run this bot's owner-only commands (identical in every bot)
- `live_message.py` — the rule that a bot message only keeps evolving while
  it is still the last thing in the chat (identical in every bot)
- `lifecycle.py` — surviving a redeploy: one poller at a time, state kept in
  Postgres, in-flight work announced (identical in every bot)
- `shared_features.py` — `/donate` (Telegram Stars) + sibling-bot cross-promotion
- `video.py` — the yt-dlp video download (Instagram/TikTok/YouTube, and
  Reddit/Twitter video posts)
- `db_merge.py` — reconciles a laptop database with the cloud one, additively (see above)
