"""
Media downloader bot -- 4th bot in the family (see ARCHITECTURE.md).
Started as Instagram/TikTok video-only; now also handles Pinterest,
Reddit, and Twitter/X.

Commands:
  /start, /help   - greeting + how it works
  /caption on|off - toggle the "⬇️ via @thisbot" credit caption
  /donate         - support hosting costs (voluntary)
  /en, /uz, /rus  - switch language (English/Uzbek/Russian); also asked
                    once, trilingually, on first /start

Paste a link at any time (no command needed):
  - Instagram / TikTok / YouTube*: video, via yt-dlp (video.py, unchanged).
  - Pinterest: a single pin's image, straight off its public og:image tag
    (no login needed) -- falls back to yt-dlp for video pins.
  - Reddit: media posts go through yt-dlp same as above (or a direct image
    fetch for plain image posts); text/link posts get rendered as a clean
    "card" image (platforms.py + cards.py) instead of just a wall of text --
    that's the actual point of downloading one of these instead of just
    screenshotting it.
  - Twitter/X: same split -- photos/video via X's own public syndication
    endpoint (what its embed widget already uses, no login) or yt-dlp for
    video; text tweets become a card. Best-effort: X can rate-limit or
    change that endpoint without notice, so this quietly falls back to
    just handing back the link rather than erroring at the user.

*YouTube is deliberately not mentioned in /help, /start, or the bot's
command menu (BOT_COMMANDS below) -- it's handled if pasted (same yt-dlp
path as everything else here) but never advertised, on purpose. Don't
"helpfully" add it to the help text later without checking with the repo
owner first -- see platforms.py's YOUTUBE_RE for why.

Requires: python-telegram-bot[job-queue]>=21.3, yt-dlp>=2024.1, httpx,
          Pillow, python-dotenv>=1.0
Env vars: DBOT_TOKEN, DBOT_USERNAME (no @), DBOT_ADMIN_ID (optional,
          comma-separated, gates /messageas, /dbdump and /status),
          DATABASE_URL and DB_SCHEMA (the shared family database, and this
          bot's schema in it), SIBLING_BOTS. Download limits and concurrency
          are documented in .env.example.
"""
import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from io import BytesIO

try:  # optional convenience: load env vars from a local .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import (
    BotCommand, InputMediaDocument, InputMediaPhoto, LinkPreviewOptions, Update,
    InlineKeyboardButton, InlineKeyboardMarkup,
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
import platforms
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
)
from video import download_video, BlockedBySource, TooLarge
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


async def _download_and_send_video(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, status=None):
    """The original Instagram/TikTok path (video.py, unmodified) -- also
    reused for YouTube, and as the fallback for Pinterest/Reddit/Twitter
    video posts. Reuses an existing status message if one's passed in,
    instead of adding a second "Downloading..." on top of whatever the
    caller already showed."""
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
        return
    if status is None:
        status = await LiveMessage.reply_to(update.message, i18n.t(lang, "downloading"))
    else:
        await status.set(context.bot, i18n.t(lang, "downloading"))

    # Say so rather than leaving them watching a "Downloading..." that has not
    # actually started yet. Only edited back afterwards if it was shown --
    # editing a message to the text it already has is a Telegram error.
    queued = _slots().locked()
    if queued:
        await status.set(context.bot, i18n.t(lang, "queued"))

    path = None
    async with _slots(), lifecycle.busy(update.effective_chat.id, i18n.t(lang, "restarting_send_again")):
        try:
            if queued:
                await status.set(context.bot, i18n.t(lang, "downloading"))
            path = await asyncio.to_thread(download_video, url, tempfile.gettempdir())
            caption = None
            if await asyncio.to_thread(get_caption_enabled, update.effective_user.id):
                caption = i18n.t(lang, "download_credit_caption", username=BOT_USERNAME)

            # Streamed from disk by python-telegram-bot rather than read into
            # memory here first.
            lossless = await asyncio.to_thread(get_lossless_enabled, update.effective_user.id)
            with open(path, "rb") as f:
                if lossless:
                    # As a document Telegram passes the file through
                    # untouched -- same container, same bitrate as yt-dlp
                    # produced. As a video it re-encodes for streaming.
                    #
                    # disable_content_type_detection is what makes that
                    # true, and leaving it off is why /lossless looked
                    # broken for videos while working for images: the Bot
                    # API sniffs an uploaded document by default, decides
                    # an .mp4 is really a video, and delivers it as one --
                    # re-encoded, which is the entire thing this branch
                    # exists to avoid. It does not do that to a .jpg,
                    # which is why the image paths were unaffected.
                    await update.message.reply_document(
                        f, filename=os.path.basename(path), caption=caption,
                        reply_markup=_nudge_kb(), read_timeout=120, write_timeout=120,
                        disable_content_type_detection=True,
                    )
                else:
                    await update.message.reply_video(
                        f, caption=caption, reply_markup=_nudge_kb(),
                        read_timeout=120, write_timeout=120,
                    )
            await status.delete(context.bot)
        except TooLarge as exc:
            await status.set(context.bot, str(exc))
            return
        except BlockedBySource as exc:
            # The site turned the *server* away. Nothing the user did is
            # wrong and nothing they can do will help, so they get a
            # sentence rather than yt-dlp's advice about exporting
            # cookies from a browser they are not using.
            #
            # Two sentences, because the two refusals are not the same
            # promise. A bot check may well pass on the next try. A login
            # wall will not pass on any try: it wants a credential this bot
            # does not have, and "try again in a bit" would be a lie.
            logger.warning("Blocked by %s (%s): %s", exc.site, exc.kind, url)
            key = ("source_needs_login" if exc.kind == "login_required"
                   else "source_blocked_server")
            await status.set(context.bot, i18n.t(lang, key), **NO_PREVIEW)
            return
        except Exception as exc:
            logger.exception("Video download failed")
            # NO_PREVIEW: an error from yt-dlp routinely carries a link to
            # its own documentation, and Telegram expands the first URL in a
            # message into a full-width preview card. The user was shown a
            # GitHub repository, with a logo, under a failed download.
            await status.set(context.bot, i18n.t(lang, "download_failed", error=exc),
                             **NO_PREVIEW)
            return
        finally:
            if path and os.path.exists(path):
                os.remove(path)

    await _after_send(update, context, lang)


async def _handle_pinterest(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    lang = await i18n.get_lang(update.effective_user.id, context)
    status = await LiveMessage.reply_to(update.message, i18n.t(lang, "fetching"))
    try:
        image_bytes = await platforms.fetch_pinterest_image(url)
    except Exception:
        logger.exception("Pinterest image fetch failed")
        image_bytes = None

    if image_bytes:
        await _reply_image(update, image_bytes, "pinterest", reply_markup=_nudge_kb())
        await status.delete(context.bot)
        await _after_send(update, context, lang)
        return

    # No og:image found -- most likely a video pin. Fall through to yt-dlp.
    await _download_and_send_video(update, context, url, status=status)


async def _handle_reddit(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
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
                image = await platforms.fetch_bytes(direct_img)
                await _reply_image(update, image, "reddit", reply_markup=_nudge_kb())
                await status.delete(context.bot)
                await _after_send(update, context, lang)
                return
            except Exception:
                logger.exception("Reddit direct image fetch failed -- falling back to yt-dlp")
        await _download_and_send_video(update, context, url, status=status)
        return

    # Text or link post -- render a card instead of just dumping the title.
    # Card meta label ("upvotes") stays English -- Pillow's built-in bitmap
    # font (cards.py) has no Cyrillic glyphs, so translating it would just
    # draw missing-glyph boxes.
    title = post.get("title", "")
    selftext = post.get("selftext", "")
    body = title if not selftext else f"{title}\n\n{selftext}"
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
    lang = await i18n.get_lang(update.effective_user.id, context)
    status = await LiveMessage.reply_to(update.message, i18n.t(lang, "fetching"))
    tweet = await platforms.fetch_tweet_syndication(url)

    has_video = bool(tweet and tweet.get("video"))
    photos = (tweet or {}).get("photos") or []

    if tweet and has_video:
        await _download_and_send_video(update, context, url, status=status)
        return

    if tweet and photos:
        try:
            # Four photos is Telegram's own album limit for a media group;
            # the old code fetched up to ten and held them all in memory to
            # send at most that many anyway.
            images = [await platforms.fetch_bytes(p["url"]) for p in photos[:4]]
            if len(images) == 1:
                await _reply_image(update, images[0], "twitter", reply_markup=_nudge_kb())
            elif await asyncio.to_thread(get_lossless_enabled, update.effective_user.id):
                await update.message.reply_media_group(
                    [
                        InputMediaDocument(
                            BytesIO(b), filename=f"twitter_{n}.{_image_extension(b)}",
                            disable_content_type_detection=True,
                        )
                        for n, b in enumerate(images, start=1)
                    ]
                )
            else:
                await update.message.reply_media_group(
                    [InputMediaPhoto(BytesIO(b)) for b in images]
                )
            await status.delete(context.bot)
            await _after_send(update, context, lang)
            return
        except Exception:
            logger.exception("Twitter photo fetch failed -- falling back to a card")

    if tweet:
        # Card meta label ("likes") stays English -- see the Reddit card
        # comment above re: Pillow's built-in font having no Cyrillic glyphs.
        user = tweet.get("user") or {}
        handle = user.get("screen_name")
        source = f"@{handle}" if handle else "Twitter/X"
        author = user.get("name", "")
        likes = tweet.get("favorite_count")
        meta = f"{likes:,} likes" if isinstance(likes, int) else ""
        png = await asyncio.to_thread(cards.render_card, "twitter", source, author, tweet.get("text", ""), meta)
        await _reply_image(update, png, "tweet", reply_markup=_nudge_kb())
        await status.delete(context.bot)
        await _after_send(update, context, lang)
        return

    # X blocked/rate-limited the fetch entirely -- don't error, just hand
    # back the link rather than pretend this failed outright.
    await status.set(context.bot, i18n.t(lang, "twitter_fetch_failed_link", url=url))


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detected = platforms.detect_platform(update.message.text)
    if not detected:
        return
    platform, url = detected

    # Asked once, here, for every platform. The gate used to live inside
    # _download_and_send_video only -- which covers Instagram, TikTok and
    # YouTube, and misses the three paths that fetch an image and send it
    # without ever reaching that function: a Pinterest pin, a Reddit image
    # post and a Twitter photo set all sailed straight through an announced
    # update. They are quicker than a video download, but "quick" is not the
    # test: a redeploy lands whenever it lands, and a fetch that started
    # ten seconds before it ends in silence exactly the same way.
    lang = await i18n.get_lang(update.effective_user.id, context)
    refusal = await refuse_new_work(lang, update.effective_user.id, update.effective_chat.id)
    if refusal:
        await update.message.reply_text(refusal)
        return

    if platform in ("instagram_tiktok", "youtube"):
        await _download_and_send_video(update, context, url)
    elif platform == "pinterest":
        await _handle_pinterest(update, context, url)
    elif platform == "reddit":
        await _handle_reddit(update, context, url)
    elif platform == "twitter":
        await _handle_twitter(update, context, url)


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
    # Before the first getUpdates: if the container this one replaces is
    # still polling, both get 409 Conflict and this bot's updates are split
    # between them. See lifecycle.py.
    await lifecycle.on_start(BOT_NAME)
    await application.bot.set_my_commands(BOT_COMMANDS)


async def _post_stop(application):
    await platforms.close_client()
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
