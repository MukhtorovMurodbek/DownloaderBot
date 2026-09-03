"""
Media downloader bot -- 4th bot in the family (see ARCHITECTURE.md).

Commands:
  /start, /help   - greeting + how it works
  /caption on|off - toggle the "via @thisbot" credit caption
  /lossless on|off- send downloads as uncompressed files
  /donate         - support hosting costs (voluntary)
  /en, /uz, /rus  - switch language (English/Uzbek/Russian); also asked
                    once, trilingually, on first /start
  /providers      - owner-only: which download route is currently working

Paste a link at any time (no command needed). Instagram, TikTok, Twitter/X,
Pinterest and Reddit all go through resolvers.py, which tries several
independent ways of getting the media and remembers which ones work -- read
that file's docstring first, because the reason it exists (a datacenter IP
gets a login page where a phone gets the post) is the single most important
thing to know about this bot.

Reddit and Twitter/X keep one thing resolvers.py does not do: a post with no
media in it is rendered as a clean card image (cards.py) instead of a wall of
text, which is the actual point of "downloading" one of those.

YouTube is deliberately not mentioned in /help, /start, or the bot's command
menu (BOT_COMMANDS below) -- it is handled if pasted but never advertised. It
is also the one platform with no route but yt-dlp, so it is the one most
likely to be refusing this server on any given day; advertising it would be
promising something the bot cannot keep. Don't "helpfully" add it to the help
text later without checking with the repo owner first.

Requires: python-telegram-bot[job-queue]>=21.3, yt-dlp>=2024.1, httpx,
          Pillow, python-dotenv>=1.0
Env vars: DBOT_TOKEN, DBOT_USERNAME (no @), DBOT_ADMIN_ID (optional,
          comma-separated, gates /messageas, /dbdump, /status, /providers),
          DATABASE_URL and DB_SCHEMA (the shared family database, and this
          bot's schema in it), SIBLING_BOTS. Download limits, concurrency and
          the per-site cookie/proxy settings are documented in .env.example.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO

try:  # optional convenience: load env vars from a local .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import (
    BotCommand, InputMediaDocument, InputMediaPhoto, InputMediaVideo,
    LinkPreviewOptions, Update, InlineKeyboardButton, InlineKeyboardMarkup,
)

# For any message whose text is an error. yt-dlp's failures quote their own
# documentation URLs, and Telegram turns the first link in a message into a
# preview card -- so a one-line "couldn't download that" arrived as a
# full-width GitHub repository with a logo, which looks far more like the bot
# breaking than like the site saying no.
NO_PREVIEW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    TypeHandler,
    filters,
)

import cards
import family_link
import i18n
import lifecycle
import net
import platforms
import resolvers
from live_message import LiveMessage, edit_in_place
from db import (
    init_db,
    get_caption_enabled,
    set_caption_enabled,
    get_lossless_enabled,
    set_lossless_enabled,
    get_user_language,
    set_user_language,
    dump_database_csv_zip,
    count_active_users_since,
    load_provider_health,
    save_provider_health,
)
from shared_features import (
    refuse_new_work,
    attach_maintenance,
    CANCEL_PICK_ALL,
    CANCEL_PICK_NONE,
    CancelItem,
    ask_cancel_choice,
    cancel_choice_key,
    cancel_items,
    cancel_shared_item,
    finish_cancel_choice,
    keep_going,
    finish_cancel,
    flush_on_shutdown,
    language_keyboard,
    tune_runtime,
    sibling_bots_blurb,
    sibling_bots_keyboard_row,
    maybe_donation_nudge,
    donate_command,
    donate_amount_chosen,
    donate_fiat_amount_chosen,
    donate_custom_button_chosen,
    donate_custom_amount_received,
    donation_precheckout_callback,
    donation_payment_callback,
    setup_logging,
    error_handler,
    track_activity,
    build_status_text,
)

setup_logging(__file__)
logger = logging.getLogger(__name__)

START_TIME = datetime.now(timezone.utc)

BOT_TOKEN = os.environ.get("DBOT_TOKEN")
BOT_USERNAME = os.environ.get("DBOT_USERNAME")  # no @


BOT_NAME = "downloaderbot"  # this bot's id within SIBLING_BOTS

# Owner-only admin tools (/messageas, /dbdump, /status) -- comma-separated
# Telegram user ids. Empty/unset means disabled for everyone.
ADMIN_IDS = {int(x) for x in os.environ.get("DBOT_ADMIN_ID", "").split(",") if x.strip()}


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def _deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """How an owner-only command answers everybody else: exactly the way a
    misspelling does.

    The intent was always to avoid confirming the command exists, and a bare
    `return` looked like the way to do that. It is not: a bot that answers
    every command it has and ignores precisely one has just pointed at the
    interesting one, and to the person who typed it the bot simply looks
    broken. Giving back the same sentence a typo gets makes the owner-only
    commands indistinguishable from commands that were never there.
    """
    await unknown_command(update, context)




# Telegram's long-poll window -- see the note in main().
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# How many downloads may be in flight at once
# ---------------------------------------------------------------------------
# This is the bot in the family with real resource spikes: yt-dlp holding a
# video, ffmpeg muxing it, and the finished file being uploaded back, all at
# once. There was no limit at all, so ten people pasting links in the same
# minute meant ten simultaneous downloads -- and on a container sized for
# one, that is the OOM kill that ARCHITECTURE.md gives as the reason this bot
# runs in a container of its own.
#
# Two at a time keeps the box responsive and costs a busy user a few seconds
# of queueing, which the "Downloading..." message already accounts for. The
# semaphore is created lazily because it must belong to the running loop.
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("DBOT_MAX_CONCURRENT_DOWNLOADS", "2"))

_download_slots: asyncio.Semaphore | None = None


def _slots() -> asyncio.Semaphore:
    global _download_slots
    if _download_slots is None:
        _download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    return _download_slots


def build_help_text(lang: str) -> str:
    return i18n.t(lang, "help_text", username=BOT_USERNAME or "this_bot") + sibling_bots_blurb(BOT_NAME, lang)


# Public command menu (the "/" button in Telegram's chat UI) -- set on
# startup via set_my_commands() below instead of pasting into @BotFather by
# hand. The owner-only /dbdump is deliberately left off. Language-switch
# commands are described in the language they switch to (self-explanatory
# by script/language, since Telegram's command menu itself isn't per-user).
BOT_COMMANDS = [
    BotCommand("start", "Start here / see the instructions"),
    BotCommand("help", "How this bot works"),
    BotCommand("caption", "Toggle the credit caption on downloads"),
    BotCommand("lossless", "Send downloads as uncompressed files"),
    BotCommand("cancel", "Stop whatever I'm waiting for"),
    BotCommand("donate", "Chip in for hosting costs"),
    BotCommand("language", "Choose your language / Tilni tanlash / Выбрать язык"),
    BotCommand("en", "Switch to English"),
    BotCommand("uz", "O'zbekchaga o'tish"),
    BotCommand("rus", "Переключиться на русский"),
]


async def _reply(update: Update, text: str, **kwargs):
    """Works whether the update came from a command or a button tap. A tap
    evolves the tapped message in place -- so choosing a language turns the
    picker itself into the instructions rather than leaving a stale picker
    sitting above them -- while a command has no earlier bot message to reuse
    and so always sends fresh.

    "In place" only holds while that message is still the last thing in the
    chat; once the user has said anything since, the answer is sent fresh
    instead of being written somewhere they have scrolled past. See
    live_message.py."""
    if update.message:
        return await LiveMessage.reply_to(update.message, text, **kwargs)
    return await edit_in_place(update.callback_query.message, update.get_bot(), text, **kwargs)


async def _continue_start(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str):
    kb = sibling_bots_keyboard_row(BOT_NAME)
    await _reply(
        update,
        i18n.t(lang, "start_greeting") + build_help_text(lang),
        reply_markup=InlineKeyboardMarkup([kb]) if kb else None,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start prints the instructions -- except the very first one, which
    asks for a language first.

    v0.7.0 made every /start the language picker, which put a three-button
    detour in front of the one command every Telegram user types by reflex,
    for the sake of a choice that is made once. The 0.6.0 shape is back, and
    the picker has moved to a command of its own:

      * No language on record -- a brand-new user -- and /start does exactly
        what /language does: greet in all three languages and ask. Picking
        one prints the instructions (see _apply_language), so the first
        /start still ends where every later one begins. This is the only
        time /start asks.
      * A language on record, and /start prints the instructions in it.
        Anyone who wants the picker back asks for it by name: /language.
    """
    lang = await asyncio.to_thread(get_user_language, update.effective_user.id)
    if lang is None:
        await update.message.reply_text(i18n.LANGUAGE_PROMPT, reply_markup=language_keyboard())
        return
    context.user_data["lang"] = lang
    await _continue_start(update, context, lang)


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/language -- the picker on demand, which is what /start used to be.

    Same trilingual greeting and same three buttons, with a tick on the
    language in force so a returning user can see which one they are on
    before deciding to change it. The tap that follows runs through
    _apply_language exactly as /en, /uz and /rus do, so it ends where they
    end: at the instructions, in the language just chosen.
    """
    lang = await asyncio.to_thread(get_user_language, update.effective_user.id)
    # Keep the cached language warm even though the picker itself is
    # trilingual -- the next handler this user hits would otherwise pay for
    # a database read that /language had already done.
    if lang:
        context.user_data["lang"] = lang
    await update.message.reply_text(i18n.LANGUAGE_PROMPT, reply_markup=language_keyboard(lang))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await i18n.get_lang(update.effective_user.id, context)
    await update.message.reply_text(build_help_text(lang))


async def _cancel_items(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> list[CancelItem]:
    """Everything this bot could be waiting on -- which is only ever the
    shared donation prompt. Spelled out anyway, and with the same signature
    the other three bots use, so that the day this bot grows a state of its
    own there is somewhere obvious for it to go."""
    return cancel_items(context, lang)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """This bot asks the fewest questions in the family -- a link needs no
    follow-up, and /caption and /lossless answer themselves with buttons --
    so the only thing /cancel usually has to catch is a donation amount it
    asked for. It exists anyway, because "does /cancel work here?" should not
    depend on which of the five bots you happen to be typing into, and
    because a forced reply left over from /donate is exactly the kind of
    thing you need a way out of. Downloads already in flight are not
    affected: those are the bot working, not the bot waiting on you.

    With one thing pending the question below is a formality here, and it is
    asked anyway: /cancel means the same thing in all four bots, and "it
    confirms except in DownloaderBot, where it just does it" is precisely
    the kind of per-bot exception this family keeps trying to stop growing.
    """
    lang = await i18n.get_lang(update.effective_user.id, context)
    if await ask_cancel_choice(update, context, await _cancel_items(update, context, lang), lang):
        return
    await finish_cancel(update, context, lang, [])


async def cancel_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A tap on one of /cancel's buttons."""
    query = update.callback_query
    lang = await i18n.get_lang(update.effective_user.id, context)
    key = cancel_choice_key(update)
    await query.answer()
    if key == CANCEL_PICK_NONE:
        await keep_going(update, context, lang)
        return
    if key == CANCEL_PICK_ALL:
        keys = [item.key for item in await _cancel_items(update, context, lang)]
    else:
        keys = [key]
    stopped = [label for label in (cancel_shared_item(context, lang, k) for k in keys) if label]
    await finish_cancel_choice(update, context, lang, stopped)


async def _apply_language(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str):
    """The single path every language change goes through -- /en, /uz, /rus
    and a tap on the picker alike -- so all four end the same way: with the
    instructions, printed in the language just chosen."""
    await asyncio.to_thread(set_user_language, update.effective_user.id, lang)
    context.user_data["lang"] = lang
    await _continue_start(update, context, lang)


async def _set_language(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str):
    await update.message.reply_text(i18n.t(lang, "language_set_confirmation"))
    await _apply_language(update, context, lang)


async def set_language_en(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_language(update, context, "en")


async def set_language_uz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_language(update, context, "uz")


async def set_language_rus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_language(update, context, "ru")


async def language_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A tap on the picker -- same effect as /en, /uz or /rus."""
    query = update.callback_query
    lang = query.data.split(":", 1)[1]
    await query.answer(i18n.t(lang, "language_set_confirmation"))
    await _apply_language(update, context, lang)


def _caption_keyboard(lang: str, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(("✅ " if enabled else "") + i18n.t(lang, "caption_state_on"), callback_data="caption:on"),
                InlineKeyboardButton(("✅ " if not enabled else "") + i18n.t(lang, "caption_state_off"), callback_data="caption:off"),
            ]
        ]
    )


async def caption_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /caption on|off still works directly for muscle memory, but bare
    # /caption now offers buttons instead of just telling you the syntax.
    lang = await i18n.get_lang(update.effective_user.id, context)
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = await asyncio.to_thread(get_caption_enabled, update.effective_user.id)
        state = i18n.t(lang, "caption_state_on" if current else "caption_state_off")
        await update.message.reply_text(
            i18n.t(lang, "caption_status", state=state),
            reply_markup=_caption_keyboard(lang, current),
        )
        return
    enabled = context.args[0].lower() == "on"
    await asyncio.to_thread(set_caption_enabled, update.effective_user.id, enabled)
    state = i18n.t(lang, "caption_state_on" if enabled else "caption_state_off")
    await update.message.reply_text(i18n.t(lang, "caption_turned", state=state))


async def caption_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = await i18n.get_lang(update.effective_user.id, context)
    enabled = query.data.split(":", 1)[1] == "on"
    await asyncio.to_thread(set_caption_enabled, update.effective_user.id, enabled)
    state = i18n.t(lang, "caption_state_on" if enabled else "caption_state_off")
    await query.answer(i18n.t(lang, "caption_toggle_answer", state=state))
    await edit_in_place(query.message, context.bot, 
        i18n.t(lang, "caption_status", state=state),
        reply_markup=_caption_keyboard(lang, enabled),
    )


def _lossless_keyboard(lang: str, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(("✅ " if enabled else "") + i18n.t(lang, "lossless_state_on"), callback_data="lossless:on"),
                InlineKeyboardButton(("✅ " if not enabled else "") + i18n.t(lang, "lossless_state_off"), callback_data="lossless:off"),
            ]
        ]
    )


async def lossless_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lossless [on|off] -- send downloads as files instead of as media.

    Telegram re-encodes anything a bot sends as a photo or a video: that
    compression is what makes it play or preview inline in the chat, and
    there is no way to ask for one without the other. Sent as a *document*
    the bytes arrive exactly as they left the source -- the original
    resolution, bitrate and container -- at the cost of a tap to open, and
    no preview in the chat list.

    So this is off by default and per-user, the same shape as /caption:
    almost everyone wants the version that plays, and the people who do not
    want it badly enough to say so. The bot's upload ceiling is unchanged
    either way (20 MB, or 50 MB behind a local Bot API server), and a file
    too big is too big in either form.
    """
    lang = await i18n.get_lang(update.effective_user.id, context)
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = await asyncio.to_thread(get_lossless_enabled, update.effective_user.id)
        state = i18n.t(lang, "lossless_state_on" if current else "lossless_state_off")
        await update.message.reply_text(
            i18n.t(lang, "lossless_status", state=state),
            reply_markup=_lossless_keyboard(lang, current),
        )
        return
    enabled = context.args[0].lower() == "on"
    await asyncio.to_thread(set_lossless_enabled, update.effective_user.id, enabled)
    state = i18n.t(lang, "lossless_state_on" if enabled else "lossless_state_off")
    await update.message.reply_text(i18n.t(lang, "lossless_turned", state=state))


async def lossless_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = await i18n.get_lang(update.effective_user.id, context)
    enabled = query.data.split(":", 1)[1] == "on"
    await asyncio.to_thread(set_lossless_enabled, update.effective_user.id, enabled)
    state = i18n.t(lang, "lossless_state_on" if enabled else "lossless_state_off")
    await query.answer(i18n.t(lang, "lossless_toggle_answer", state=state))
    await edit_in_place(query.message, context.bot, 
        i18n.t(lang, "lossless_status", state=state),
        reply_markup=_lossless_keyboard(lang, enabled),
    )


async def dbdump_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dbdump -- exports every table in this bot's own database as one
    zip of CSVs. Owner-only."""
    if not _is_admin(update.effective_user.id):
        return await _deny(update, context)
    status = await LiveMessage.reply_to(update.message, "Exporting the database...")
    try:
        data = await asyncio.to_thread(dump_database_csv_zip)
    except Exception as exc:
        await status.set(context.bot, f"⚠️ Export failed: {exc}")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    await update.message.reply_document(document=BytesIO(data), filename=f"downloaderbot_db_{stamp}.zip")
    await status.delete(context.bot)


async def messageas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/messageas <user_id> <text> -- sends a message to that user as this
    bot. Only works if the user has messaged the bot before (Telegram
    doesn't let bots cold-message anyone). Owner-only for obvious reasons."""
    if not _is_admin(update.effective_user.id):
        return await _deny(update, context)

    if len(context.args) < 2 or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /messageas <user_id> <message text>")
        return

    user_id = int(context.args[0])
    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
        await update.message.reply_text("✅ Sent.")
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Couldn't send it: {exc}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status -- uptime, hosting environment, any crashes since this
    process started, and active-user counts. Owner-only, same reasoning as
    /dbdump: this is operational info, not something every user should see."""
    if not _is_admin(update.effective_user.id):
        return await _deny(update, context)
    now = datetime.now(timezone.utc)
    users_hour = await asyncio.to_thread(count_active_users_since, now - timedelta(hours=1))
    users_since_start = await asyncio.to_thread(count_active_users_since, START_TIME)
    await update.message.reply_text(build_status_text(START_TIME, users_hour, users_since_start))


# How often the in-memory provider scores are written to Postgres. A minute
# is short enough that a crash loses nothing that matters and long enough that
# a busy hour is sixty writes rather than a write per download. See
# resolvers.take_dirty().
PROVIDER_HEALTH_FLUSH_SECONDS = int(
    os.environ.get("DBOT_PROVIDER_FLUSH_SECONDS", "60"))


async def _flush_provider_health(_context=None) -> None:
    dirty = resolvers.take_dirty()
    if not dirty:
        return
    rows = [(name, h.ok, h.failed, h.streak, h.last_ok, h.last_fail, h.last_error)
            for name, h in dirty.items()]
    try:
        await asyncio.to_thread(save_provider_health, rows)
    except Exception:
        # Scores are advice, not user data. If the database is unreachable the
        # bot carries on choosing providers from memory rather than failing a
        # download over a bookkeeping write.
        logger.warning("couldn't save provider health", exc_info=True)


# How long one route gets during a bus probe. Shorter than a real download's
# PROVIDER_TIMEOUT, because ParentBot calls a family command failed after 90
# seconds and a probe nobody can run from their phone is not worth having.
PROBE_TIMEOUT_S = float(os.environ.get("DBOT_PROBE_TIMEOUT", "12"))


async def _bus_probe(context, args):
    """`probe [url|platform ...]` over the family bus.

    THE POINT OF THIS COMMAND. Every route in resolvers.py was verified from
    the developer's laptop, on a residential connection -- and the entire
    reason those routes exist is that a residential address and a datacenter
    address get different answers from the same site. So a laptop test proves
    the code and proves nothing about production. This runs the same checks
    from inside the container that actually serves users, which is the only
    place the answer means anything.

    It asks EVERY route rather than stopping at the first that works, and it
    deliberately leaves the health scores alone: a probe is a question, not
    traffic, and an operator's curiosity should not reorder the chain real
    users get.

    Defaults to the sample links in resolvers.SAMPLES. Name platforms
    ("probe instagram tiktok") to narrow it, or paste real links to probe
    those instead -- one per platform, since the later one wins.
    """
    fetch = True
    urls: dict[str, str] = {}
    for arg in args:
        if arg in ("--nofetch", "nofetch"):
            fetch = False
            continue
        if arg in resolvers.SAMPLES:
            urls[arg] = resolvers.SAMPLES[arg]
            continue
        detected = platforms.detect_platform(arg)
        if detected:
            urls[detected[0]] = detected[1]
    results = await resolvers.probe_all(urls or None, fetch=fetch,
                                        timeout=PROBE_TIMEOUT_S)
    header = ("Download routes, asked from inside this container "
              f"({os.environ.get('RAILWAY_ENVIRONMENT') or 'local'}). "
              "Scores untouched.\n")
    return header + resolvers.format_probe(results), None, None


async def providers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/providers -- which download route is working and which is not.

    Owner-only, same reasoning as /dbdump and /status: operational detail, and
    left in English like the rest of the admin output. This is the command
    that answers "Instagram broke again, what happened" without reading logs.
    """
    if not _is_admin(update.effective_user.id):
        return await _deny(update, context)
    now = time.time()
    lines = []
    for platform, chain in resolvers.PROVIDERS.items():
        lines.append(f"\n{platform}:")
        for name, _fn in chain:
            h = resolvers.health(name)
            if h.ok == 0 and h.failed == 0:
                lines.append(f"  \u2022 {name} -- not tried yet")
                continue
            if h.streak == 0 and h.ok:
                mark, note = "\u2705", f"last ok {_ago(now - (h.last_ok or now))} ago"
            elif h.cooling_until > now:
                mark = "\u274c"
                note = (f"{h.streak} in a row, resting "
                        f"{_ago(h.cooling_until - now)}: "
                        f"{resolvers.tidy_error(h.last_error)}")
            else:
                mark = "\u26a0\ufe0f"
                note = f"{h.streak} in a row: {resolvers.tidy_error(h.last_error)}"
            lines.append(f"  {mark} {name} -- {h.ok} ok / {h.failed} failed, {note}")
    await update.message.reply_text(
        "Download routes, best first per platform:" + "\n".join(lines),
        **NO_PREVIEW)


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _nudge_kb() -> InlineKeyboardMarkup | None:
    # Point at ConvertBot too -- e.g. someone might want just the audio, or
    # a gif, out of what they just downloaded. No file handoff between bots
    # anymore (each bot is fully independent) -- they re-send it there.
    kb_row = sibling_bots_keyboard_row(BOT_NAME, only="convertbot")
    return InlineKeyboardMarkup([kb_row]) if kb_row else None


async def _after_send(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str):
    nudge = await maybe_donation_nudge(update.effective_user.id, lang)
    if nudge:
        await update.message.reply_text(nudge)


def _image_extension(data: bytes) -> str:
    """Name a file after what it actually is.

    Telegram shows a document by its filename, and there is nothing in a
    download's bytes to say what the source called it -- so a .jpg that is
    really a .webp is exactly the sort of small lie that has people
    believing the bot mangled their picture. Four magic numbers cover
    everything these platforms serve.
    """
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"GIF8"):
        return "gif"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "bin"


async def _reply_image(update: Update, data: bytes, stem: str, reply_markup=None):
    """One image back, compressed as a photo or verbatim as a file -- see
    lossless_toggle for which and why."""
    if await asyncio.to_thread(get_lossless_enabled, update.effective_user.id):
        return await update.message.reply_document(
            document=BytesIO(data),
            filename=f"{stem}.{_image_extension(data)}",
            reply_markup=reply_markup,
            disable_content_type_detection=True,
        )
    return await update.message.reply_photo(BytesIO(data), reply_markup=reply_markup)


async def _send_media(update, context, items_with_paths, lang) -> None:
    """Put what was resolved into the chat.

    One file goes as a photo or a video so it plays inline; several go as an
    album, in Telegram's own maximum of ten per group. Under /lossless every
    one of them goes as a document instead -- see lossless_toggle for the
    whole argument, and note `disable_content_type_detection`, without which
    the Bot API sniffs an uploaded .mp4, decides it is really a video, and
    delivers it re-encoded, which is the entire thing that branch exists to
    avoid.
    """
    caption = None
    if await asyncio.to_thread(get_caption_enabled, update.effective_user.id):
        caption = i18n.t(lang, "download_credit_caption", username=BOT_USERNAME)
    lossless = await asyncio.to_thread(get_lossless_enabled, update.effective_user.id)

    if len(items_with_paths) == 1:
        item, path = items_with_paths[0]
        # Streamed from disk by python-telegram-bot rather than read into
        # memory here first.
        with open(path, "rb") as f:
            if lossless:
                await update.message.reply_document(
                    f, filename=os.path.basename(item.filename or path),
                    caption=caption, reply_markup=_nudge_kb(),
                    read_timeout=120, write_timeout=120,
                    disable_content_type_detection=True,
                )
            elif item.kind == "video":
                await update.message.reply_video(
                    f, caption=caption, reply_markup=_nudge_kb(),
                    read_timeout=120, write_timeout=120,
                )
            else:
                await update.message.reply_photo(
                    f, caption=caption, reply_markup=_nudge_kb(),
                    read_timeout=120, write_timeout=120,
                )
        return

    # An album. The caption rides on the first item of the first group, which
    # is where Telegram shows an album's caption.
    for chunk_start in range(0, len(items_with_paths), 10):
        chunk = items_with_paths[chunk_start:chunk_start + 10]
        handles = [open(path, "rb") for _, path in chunk]
        try:
            group = []
            for n, ((item, path), handle) in enumerate(zip(chunk, handles)):
                cap = caption if (chunk_start == 0 and n == 0) else None
                if lossless:
                    group.append(InputMediaDocument(
                        handle, filename=os.path.basename(item.filename or path),
                        caption=cap, disable_content_type_detection=True))
                elif item.kind == "video":
                    group.append(InputMediaVideo(handle, caption=cap))
                else:
                    group.append(InputMediaPhoto(handle, caption=cap))
            await update.message.reply_media_group(
                group, read_timeout=120, write_timeout=120)
        finally:
            for handle in handles:
                handle.close()


async def _resolve_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            platform: str, url: str, status=None,
                            quiet_if_missing: bool = False) -> bool:
    """The whole download path: pick a route that is working, fetch, send.

    Returns True if something was delivered. On failure the user has already
    been told, with one exception: `quiet_if_missing` says the caller has a
    better answer than an error for a post with no media in it -- a text
    tweet, which becomes a card instead.

    Reuses an existing status message if one is passed in, instead of stacking
    a second "Fetching..." on top of whatever the caller already showed.
    """
    lang = await i18n.get_lang(update.effective_user.id, context)
    # A download outlasts the shutdown grace period, so starting one now would
    # end in silence. A few seconds' wait beats a lost file.
    refusal = await refuse_new_work(
        lang, update.effective_user.id, update.effective_chat.id
    )
    if refusal:
        if status is None:
            await update.message.reply_text(refusal)
        else:
            await status.set(context.bot, refusal)
        return False

    if status is None:
        status = await LiveMessage.reply_to(update.message, i18n.t(lang, "fetching"))
    else:
        await status.set(context.bot, i18n.t(lang, "fetching"))

    # Say so rather than leaving them watching a "Fetching..." that has not
    # actually started yet.
    if _slots().locked():
        await status.set(context.bot, i18n.t(lang, "queued"))

    paths: list[str] = []
    async with _slots(), lifecycle.busy(update.effective_chat.id,
                                        i18n.t(lang, "restarting_send_again")):
        try:
            resolved = await resolvers.resolve(platform, url)
        except resolvers.NothingWorked as exc:
            logger.info("no route worked for %s: %s", platform, exc)
            if exc.kind == "missing" and quiet_if_missing:
                return False
            key = {
                "missing": "download_missing",
                "blocked": "download_blocked",
                "too_big": "download_too_big",
            }.get(exc.kind, "download_all_routes_failed")
            # NO_PREVIEW: yt-dlp's failures quote their own documentation URLs
            # and Telegram expands the first link in a message, so a one-line
            # "couldn't download that" once arrived as a full-width GitHub
            # repository, logo and all, underneath a failed download.
            await status.set(context.bot, i18n.t(lang, key), **NO_PREVIEW)
            return False

        try:
            await status.set(context.bot, i18n.t(lang, "downloading"))
            items = resolved.items
            for item in items:
                paths.append(await resolvers.download(item))
            await _send_media(update, context, list(zip(items, paths)), lang)
            await status.delete(context.bot)
        except net.FetchError as exc:
            await status.set(context.bot, str(exc), **NO_PREVIEW)
            return False
        except Exception as exc:
            logger.exception("Delivering %s via %s failed", platform, resolved.provider)
            await status.set(context.bot, i18n.t(lang, "download_failed", error=exc),
                             **NO_PREVIEW)
            return False
        finally:
            for path in paths:
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except OSError:
                    logger.warning("couldn't remove %s", path)

    await _after_send(update, context, lang)
    return True


async def _handle_reddit(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Reddit is the one platform where the *text* is often the point, so the
    post is read first and only media posts go near the download path."""
    lang = await i18n.get_lang(update.effective_user.id, context)
    status = await LiveMessage.reply_to(update.message, i18n.t(lang, "fetching"))
    try:
        post = await platforms.fetch_reddit_post(url)
    except Exception as exc:
        await status.set(context.bot, i18n.t(lang, "reddit_fetch_failed", error=exc))
        return

    if platforms.reddit_is_media(post):
        direct_img = platforms.reddit_direct_image_url(post)
        if direct_img:
            try:
                image = await net.fetch_bytes(direct_img)
                await _reply_image(update, image, "reddit", reply_markup=_nudge_kb())
                await status.delete(context.bot)
                await _after_send(update, context, lang)
                return
            except Exception:
                logger.exception("Reddit direct image fetch failed -- falling back")
        await _resolve_and_send(update, context, "reddit", url, status=status)
        return

    # Text or link post -- render a card instead of just dumping the title.
    # Card meta label ("upvotes") stays English -- Pillow's built-in bitmap
    # font (cards.py) has no Cyrillic glyphs, so translating it would just
    # draw missing-glyph boxes.
    title = post.get("title", "")
    selftext = post.get("selftext", "")
    body = title if not selftext else title + "\n\n" + selftext
    subreddit = "r/" + post.get("subreddit", "?")
    author = "u/" + post.get("author", "?")
    score = post.get("score")
    meta = f"{score:,} upvotes" if isinstance(score, int) else ""

    png = await asyncio.to_thread(cards.render_card, "reddit", subreddit, author, body, meta)
    # The cards go through the same switch as the downloads. A card is text
    # rendered as an image, which is the content photo compression damages
    # most visibly, so "lossless" meaning "except the ones with words on"
    # would be the wrong kind of surprising.
    await _reply_image(update, png, "reddit_post", reply_markup=_nudge_kb())
    await status.delete(context.bot)
    await _after_send(update, context, lang)


async def _handle_twitter(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Media first, card second.

    Before v1.2.1 this asked X's syndication endpoint what the tweet
    contained and then sent yt-dlp after the video, so a video tweet needed
    both of them to be working at once. Now the provider chain answers the
    media question on its own, and syndication is consulted only for the
    tweets that have no media -- the one thing it is still needed for, and
    the case it has always been most reliable at.
    """
    lang = await i18n.get_lang(update.effective_user.id, context)
    status = await LiveMessage.reply_to(update.message, i18n.t(lang, "fetching"))
    if await _resolve_and_send(update, context, "twitter", url, status=status,
                               quiet_if_missing=True):
        return

    tweet = await platforms.fetch_tweet_syndication(url)
    if tweet:
        # Card meta label ("likes") stays English -- see the Reddit card
        # comment above re: Pillow's built-in font having no Cyrillic glyphs.
        user = tweet.get("user") or {}
        handle = user.get("screen_name")
        source = f"@{handle}" if handle else "Twitter/X"
        author = user.get("name", "")
        likes = tweet.get("favorite_count")
        meta = f"{likes:,} likes" if isinstance(likes, int) else ""
        png = await asyncio.to_thread(
            cards.render_card, "twitter", source, author, tweet.get("text", ""), meta)
        await _reply_image(update, png, "tweet", reply_markup=_nudge_kb())
        await status.delete(context.bot)
        await _after_send(update, context, lang)
        return

    # X blocked the fetch entirely -- don't error, just hand back the link
    # rather than pretend this failed outright.
    await status.set(context.bot, i18n.t(lang, "twitter_fetch_failed_link", url=url))


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detected = platforms.detect_platform(update.message.text)
    if not detected:
        return
    platform, url = detected

    # Asked once, here, for every platform. The gate used to live inside the
    # video path only -- which covers Instagram, TikTok and YouTube, and
    # misses the paths that fetch an image and send it without ever reaching
    # that function: a Pinterest pin, a Reddit image post and a Twitter photo
    # set all sailed straight through an announced update. They are quicker
    # than a video download, but "quick" is not the test: a redeploy lands
    # whenever it lands, and a fetch that started ten seconds before it ends
    # in silence exactly the same way.
    lang = await i18n.get_lang(update.effective_user.id, context)
    refusal = await refuse_new_work(lang, update.effective_user.id, update.effective_chat.id)
    if refusal:
        await update.message.reply_text(refusal)
        return

    if platform == "reddit":
        await _handle_reddit(update, context, url)
    elif platform == "twitter":
        await _handle_twitter(update, context, url)
    else:
        await _resolve_and_send(update, context, platform, url)


async def unrecognized_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches anything that isn't a recognized link and isn't a known
    command -- registered last, so it only fires when nothing else already
    handled the update. Silence here would just look like the bot ignored
    them."""
    lang = await i18n.get_lang(update.effective_user.id, context)
    await update.message.reply_text(i18n.t(lang, "unrecognized_message"))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await i18n.get_lang(update.effective_user.id, context)
    await update.message.reply_text(i18n.t(lang, "unknown_command"))


async def _post_init(application):
    await tune_runtime(application)
    # Which download routes were working when this container's predecessor
    # stopped. Without it a bot that redeploys several times a day re-learns
    # that a dead service is dead every single time, at a user's expense.
    try:
        resolvers.load(await asyncio.to_thread(load_provider_health))
    except Exception:
        logger.warning("couldn't load provider health -- starting from scratch",
                       exc_info=True)
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            _flush_provider_health,
            interval=PROVIDER_HEALTH_FLUSH_SECONDS,
            first=PROVIDER_HEALTH_FLUSH_SECONDS,
        )
    # Before the first getUpdates: if the container this one replaces is
    # still polling, both get 409 Conflict and this bot's updates are split
    # between them. See lifecycle.py.
    await lifecycle.on_start(BOT_NAME)
    await application.bot.set_my_commands(BOT_COMMANDS)


async def _post_stop(application):
    # Before the pool closes: this is the one write that would otherwise lose
    # a whole flush interval of "this route just started failing".
    await _flush_provider_health()
    await net.close_client()
    # Before flush_on_shutdown, which closes the pool lifecycle writes through.
    await lifecycle.on_stop(application)
    await flush_on_shutdown(application)


def main():
    if not BOT_TOKEN or not BOT_USERNAME:
        raise SystemExit("Set DBOT_TOKEN and DBOT_USERNAME environment variables first.")

    init_db()
    builder = (
        ApplicationBuilder().token(BOT_TOKEN)
        .post_init(_post_init).post_stop(_post_stop)
    )
    # Language, toggles and anything half-finished, kept in Postgres so a
    # redeploy is not visible to whoever was mid-download. See lifecycle.py.
    state = lifecycle.persistence()
    if state is not None:
        builder = builder.persistence(state)
    app = builder.build()
    lifecycle.install(app, BOT_NAME)
    app.add_error_handler(error_handler)
    app.add_handler(TypeHandler(Update, track_activity), group=-1)
    # Runs after track_activity but before every other handler -- a no-op
    # unless a "Custom" donate button was just tapped, in which case it
    # consumes the reply and stops it from also being treated as a normal
    # message (see donate_custom_amount_received's docstring).
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, donate_custom_amount_received), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("caption", caption_toggle))
    app.add_handler(CommandHandler("lossless", lossless_toggle))
    app.add_handler(CommandHandler("dbdump", dbdump_command))  # owner-only
    app.add_handler(CommandHandler("messageas", messageas_command))  # owner-only
    app.add_handler(CommandHandler("status", status_command))  # owner-only
    app.add_handler(CommandHandler("providers", providers_command))  # owner-only
    app.add_handler(CallbackQueryHandler(caption_toggle_callback, pattern="^caption:"))
    app.add_handler(CallbackQueryHandler(lossless_toggle_callback, pattern="^lossless:"))
    app.add_handler(CallbackQueryHandler(cancel_choice_callback, pattern="^cancelpick:"))
    app.add_handler(CallbackQueryHandler(language_chosen, pattern="^setlang:"))
    app.add_handler(CommandHandler("language", language_command))

    # ---- language: /en, /uz, /rus -- pick at first /start, change anytime ----
    app.add_handler(CommandHandler("en", set_language_en))
    app.add_handler(CommandHandler("uz", set_language_uz))
    app.add_handler(CommandHandler("rus", set_language_rus))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(platforms.ANY_LINK_RE), handle_link))

    # ---- donations (Telegram Stars) -- this bot's only Stars usage ----
    app.add_handler(CommandHandler("donate", donate_command))
    app.add_handler(CallbackQueryHandler(donate_amount_chosen, pattern="^donate:"))
    app.add_handler(CallbackQueryHandler(donate_fiat_amount_chosen, pattern="^donatefiat:"))
    app.add_handler(CallbackQueryHandler(donate_custom_button_chosen, pattern="^donatecustom:"))
    app.add_handler(PreCheckoutQueryHandler(donation_precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, donation_payment_callback))

    # Both registered last, so every real handler above gets first shot.
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unrecognized_message))

    # ParentBot's link: heartbeats, crash/donation events, and the queue it
    # uses to run this bot's owner-only commands remotely. Never raises --
    # with no shared database reachable the bot just runs on its own.
    family_link.attach(app, BOT_NAME, "DownloaderBot", START_TIME)
    # One extra bus command, registered here rather than in family_link.py:
    # only this bot has download routes to probe, and family_link.py has to
    # stay byte-identical across all five. The dispatcher looks COMMANDS up
    # per command, so adding to it after import is enough.
    family_link.COMMANDS["probe"] = _bus_probe
    family_link.COMMAND_HELP["probe"] = (
        "probe [platform|url ...] -- try every download route from in here")
    attach_maintenance(app)

    logger.info("Bot starting (polling)...")
    # A 30-second long poll is the same latency as the default 10 -- Telegram
    # answers the moment an update exists -- for a third of the HTTP requests.
    # allowed_updates lists every kind this bot has a handler for, so Telegram
    # stops sending the rest rather than this process parsing and dropping it.
    app.run_polling(**lifecycle.polling_kwargs(
        timeout=POLL_TIMEOUT,
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY, Update.PRE_CHECKOUT_QUERY],
    ))


if __name__ == "__main__":
    main()
