"""Small self-contained features used by this bot -- kept in one file for
convenience. It is copied byte-identically into every bot rather than
imported across folders, because each bot is its own repo and its own
deployment -- the same arrangement family_link.py uses. Nothing here
touches another bot's data: the cross-bot machinery all lives in
family_link.py. Things that live here:
  1. Sibling-bot cross-promotion text/buttons, for /start and /help --
     purely cosmetic (one env var), no database involved. Plus the one-tap
     language picker /start shows, which every bot renders the same way.
  2. A throttled, non-annoying donation reminder + a self-serve /donate
     command paid in Telegram Stars (no external payment processor needed).
  3. The shared half of /cancel: the states this file can leave a user
     waiting in, the release of Telegram's client-side reply lock, and the
     one report format every bot's /cancel answers in.
  4. Logging setup, unhandled-exception tracking, active-user tracking, and
     hosting-environment detection, all in support of each bot's owner-only
     /status command.
  5. attach_maintenance()/flush_on_shutdown(): the jobs that keep a
     long-running process cheap -- writing buffered activity counts out in
     batches instead of a row per update, and dropping the cached per-user
     state of people who stopped using the bot months ago.
"""
import asyncio
import logging
import os
import socket
import time
import traceback
import uuid
from collections import deque, namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from telegram import (
    ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    ReplyKeyboardRemove,
)
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ApplicationHandlerStop

import db
import i18n

# ---------------------------------------------------------------------------
# Sibling-bot cross-promotion
# ---------------------------------------------------------------------------
# SIBLING_BOTS format (one env var, same value given to every bot in the
# family): "id:Display Name:username,id:Display Name:username"
#   e.g. "stickerbot:StickerBot:MyStickerBot,convertbot:ConvertBot:MyConvertBot"
# Each bot filters *itself* out of the list using its own BOT_NAME constant.
# This is cosmetic only -- purely a shared env var, not a shared database.

def _parse_sibling_bots() -> list[dict]:
    raw = os.environ.get("SIBLING_BOTS", "")
    bots = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            continue
        bot_id, name, username = parts
        bots.append({"id": bot_id, "name": name, "username": username})
    return bots


def sibling_bots_blurb(this_bot_name: str, lang: str) -> str:
    """One short pointer line, not a repeat of the sibling list itself --
    sibling_bots_keyboard_row() below already renders that list as buttons,
    so spelling it out again in text too was just duplicate noise."""
    others = [b for b in _parse_sibling_bots() if b["id"] != this_bot_name]
    if not others:
        return ""
    return i18n.t(lang, "sibling_blurb")


def sibling_bots_keyboard_row(this_bot_name: str, only: str | None = None) -> list[InlineKeyboardButton]:
    others = [b for b in _parse_sibling_bots() if b["id"] != this_bot_name]
    if only:
        others = [b for b in others if b["id"] == only]
    return [InlineKeyboardButton(f"↗️ {b['name']}", url=f"https://t.me/{b['username']}") for b in others]


# ---------------------------------------------------------------------------
# The language picker
# ---------------------------------------------------------------------------
# The trilingual greeting used to end with "/en — English, /uz — O'zbekcha,
# /rus — Русский" and wait for the user to type one. Asking someone to type a
# command before they have been told what the bot does is the worst possible
# first impression, and on a phone it is three taps and a keyboard. One row of
# buttons is one tap, and the /en /uz /rus commands still work for anyone who
# has learned them.
#
# This is what a brand-new user's first /start shows, and what /language
# shows on demand at any point after that -- see start() and
# language_command() in each bot. Passing `current` ticks the language
# already in force, so a returning user can see at a glance which one they
# are on before deciding to change it.

def language_keyboard(current: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("✅ " if code == current else "") + label,
                             callback_data=f"setlang:{code}")
        for code, label in i18n.LANGUAGE_LABELS.items()
    ]])


# ---------------------------------------------------------------------------
# Runtime tuning
# ---------------------------------------------------------------------------

WORKER_THREADS = int(os.environ.get("WORKER_THREADS", "4"))


async def tune_runtime(application) -> None:
    """Call from each bot's post_init.

    asyncio's default executor sizes itself to min(32, cpu_count + 4), and on
    a shared cloud host cpu_count is the *machine's* core count rather than
    this container's slice of it. That is up to 32 threads, each with its own
    stack, standing by for a workload whose peak is a couple of concurrent
    database calls and one ffmpeg. Four is plenty, and the difference is
    real resident memory on the smallest plan that fits."""
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="worker")
    )


# ---------------------------------------------------------------------------
# Donation reminder + /donate (Telegram Stars)
# ---------------------------------------------------------------------------
# Cadence: shown after DONATION_ACTION_THRESHOLD successful actions (each
# bot decides what counts as an "action" -- a finished pack, a completed
# conversion, etc -- and calls maybe_donation_nudge() at that point) OR
# after DONATION_MIN_DAYS_BETWEEN days of not having seen it (so light users
# get reminded occasionally too) -- but never twice within
# DONATION_COOLDOWN_FLOOR_DAYS regardless, so someone who blitzes through 20
# actions in one sitting only sees it once. The counter/cooldown live in
# THIS bot's own schema only -- the five bots share one database but never
# each other's tables, so a user active in two bots in the same week may see
# the nudge from each of them; that's an accepted tradeoff. All three
# numbers are just constants -- tune freely, same as the Stars price tiers
# elsewhere.

DONATION_ACTION_THRESHOLD = 20
DONATION_MIN_DAYS_BETWEEN = 14
DONATION_COOLDOWN_FLOOR_DAYS = 3

DONATE_STAR_OPTIONS = [15, 50, 100]  # preset amounts shown as buttons on /donate
# Loose sanity ceiling for /donate <amount> -- not Telegram's real per-invoice
# cap (which Telegram enforces itself; a rejected amount just surfaces
# Telegram's own error text back to the sender), just a guard against an
# obvious typo like an extra zero or two.
MAX_DONATION_STARS = 100_000

# ---- optional fiat alternative (USD) to Stars ----
# Cashing Stars out to real money goes through Fragment (Stars -> TON ->
# sell on an exchange) -- several hops for something as simple as "cover the
# hosting bill." A regular Telegram Payments provider skips that: connect
# one via @BotFather -> /mypayments (Stripe or another provider that
# supports your country), then set PAYMENT_PROVIDER_TOKEN_USD. Nothing below
# activates until that's set -- with it unset, /donate behaves exactly as it
# did before (Stars only).
#
# "exp"/min/max here are straight from Telegram's own reference table
# (https://core.telegram.org/bots/payments/currencies.json, checked
# 2026-08-24) -- invoice amounts must be in the currency's smallest unit
# (10**exp per whole unit), e.g. $5.00 -> 500. Don't add another currency
# here without checking that table for its real exp -- guessing it risks
# over/undercharging someone by a factor of 100. Its options list must stay
# the same length as DONATE_STAR_OPTIONS -- donate_command() zips them
# together into one Stars-column-then-fiat-column button grid per row.
FIAT_CURRENCIES = {
    "USD": {
        "symbol": "$", "label": "USD", "exp": 2,
        "options": [1, 5, 10], "min_minor": 100, "max_minor": 1_000_000,
    },
}


def _fiat_provider_token(currency: str) -> str | None:
    return os.environ.get(f"PAYMENT_PROVIDER_TOKEN_{currency}") or None


def _available_fiat_currencies() -> list[str]:
    return [c for c in FIAT_CURRENCIES if _fiat_provider_token(c)]


def format_ledger_amount(amount: int, currency: str) -> str:
    """amount is in the currency's smallest unit (see FIAT_CURRENCIES) for
    fiat, or a plain Stars count for XTR."""
    if currency == "XTR":
        return f"{amount}⭐"
    cfg = FIAT_CURRENCIES.get(currency)
    if not cfg:
        return f"{amount} {currency}"
    return f"{amount / (10 ** cfg['exp']):g} {cfg['symbol']}"


def _donation_nudge_due(user_id: int) -> bool:
    """Blocking; the async wrapper below is what handlers call. One statement
    to bump-and-read, and a second one only in the rare case where the nudge
    actually fires."""
    count, last_shown = db.bump_donation_action(user_id)

    days_since = None
    if last_shown:
        days_since = (datetime.now(timezone.utc) - datetime.fromisoformat(last_shown)).days
        if days_since < DONATION_COOLDOWN_FLOOR_DAYS:
            return False  # hard floor -- never nag twice in quick succession

    hit_action_threshold = count >= DONATION_ACTION_THRESHOLD
    # Time alone shouldn't fire for someone who's barely touched the bot --
    # require at least a handful of actions too.
    hit_time_threshold = (days_since is None or days_since >= DONATION_MIN_DAYS_BETWEEN) and count >= 3

    if hit_action_threshold or hit_time_threshold:
        db.reset_donation_prompt(user_id)
        return True
    return False


async def maybe_donation_nudge(user_id: int, lang: str) -> str | None:
    """Await right after a successful action. Returns text to append to your
    reply, or None most of the time (send nothing).

    Async because it writes to the database, and this is called on the
    success path of the bot's main job -- doing it inline on the event loop
    stalls every other user's update for the round trip."""
    try:
        due = await asyncio.to_thread(_donation_nudge_due, user_id)
    except Exception:
        logging.getLogger(__name__).debug("Donation nudge check failed", exc_info=True)
        return None
    return i18n.t(lang, "donation_nudge") if due else None


async def _send_donation_invoice(chat_id: int, user, context, amount: int, lang: str, currency: str = "XTR") -> str | None:
    """amount is in the currency's smallest unit for fiat (see
    FIAT_CURRENCIES), or a plain Stars count for XTR. Returns None on
    success, or an error message to show the sender if Telegram itself
    rejects the amount/currency (e.g. above Telegram's own per-invoice cap,
    or no provider connected for that currency)."""
    payload = f"donate:{uuid.uuid4().hex}"
    await asyncio.to_thread(
        db.record_star_invoice, user.id, user.username, amount, "donation", payload,
        "invoiced", currency,
    )
    provider_token = "" if currency == "XTR" else (_fiat_provider_token(currency) or "")
    try:
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=i18n.t(lang, "donate_invoice_title"),
            description=i18n.t(lang, "donate_invoice_description"),
            payload=payload,
            provider_token=provider_token,  # empty string is required for Telegram Stars payments
            currency=currency,
            prices=[LabeledPrice(i18n.t(lang, "donate_invoice_label"), amount)],
        )
        return None
    except Exception as exc:
        await asyncio.to_thread(db.update_star_transaction, payload, "failed")
        return i18n.t(lang, "donate_invoice_error", error=exc)


def _validate_donation_amount(whole_amount: int, currency: str, lang: str) -> tuple[int | None, str | None]:
    """whole_amount is in whole units (a Stars count, or whole dollars for
    USD). Returns (amount in the invoice's own unit, None) on success, or
    (None, error message) if it's outside the currency's allowed range."""
    if currency == "XTR":
        if whole_amount > MAX_DONATION_STARS:
            return None, i18n.t(lang, "donate_too_many_stars", max=MAX_DONATION_STARS)
        return whole_amount, None
    cfg = FIAT_CURRENCIES[currency]
    amount = whole_amount * (10 ** cfg["exp"])
    if amount < cfg["min_minor"] or amount > cfg["max_minor"]:
        lo = cfg["min_minor"] / (10 ** cfg["exp"])
        hi = cfg["max_minor"] / (10 ** cfg["exp"])
        return None, i18n.t(lang, "donate_out_of_range", currency=currency, lo=f"{lo:g}", hi=f"{hi:g}", symbol=cfg["symbol"])
    return amount, None


async def donate_command(update, context) -> None:
    """/donate -- with no args, shows preset-amount buttons for Stars plus
    any fiat currency that has a payment provider connected (see
    FIAT_CURRENCIES above), with a Custom button per currency for anything
    else. /donate <amount> [currency] skips straight to an invoice --
    currency defaults to Stars, e.g. /donate 500 or /donate 5 usd."""
    lang = await i18n.get_lang(update.effective_user.id, context)
    if context.args:
        raw = context.args[0].replace(",", "")
        currency = "XTR"
        if len(context.args) > 1:
            requested = context.args[1].upper()
            if requested not in FIAT_CURRENCIES:
                await update.message.reply_text(i18n.t(lang, "donate_unknown_currency", currency=context.args[1]))
                return
            if not _fiat_provider_token(requested):
                await update.message.reply_text(i18n.t(lang, "donate_currency_not_configured", currency=requested))
                return
            currency = requested
        if not raw.lstrip("-").isdigit() or int(raw) <= 0:
            await update.message.reply_text(i18n.t(lang, "donate_invalid_amount"))
            return

        amount, validation_error = _validate_donation_amount(int(raw), currency, lang)
        if validation_error:
            await update.message.reply_text(validation_error)
            return

        error = await _send_donation_invoice(update.effective_chat.id, update.effective_user, context, amount, lang, currency)
        if error:
            await update.message.reply_text(error)
        return

    # One row per preset tier, Stars in the left column and each connected
    # fiat currency in its own column to the right -- e.g. with just USD
    # connected: [15⭐ 1$] / [50⭐ 5$] / [100⭐ 10$] -- plus a final row of
    # Custom buttons, one per column, for anything not on the list.
    fiat_options = _available_fiat_currencies()
    columns = [("XTR", DONATE_STAR_OPTIONS, "⭐")] + [
        (ccy, FIAT_CURRENCIES[ccy]["options"], FIAT_CURRENCIES[ccy]["symbol"]) for ccy in fiat_options
    ]
    kb_rows = [
        [
            InlineKeyboardButton(
                f"{amount} {symbol}",
                callback_data=f"donate:{amount}" if ccy == "XTR" else f"donatefiat:{ccy}:{amount}",
            )
            for (ccy, _, symbol), amount in zip(columns, row)
        ]
        for row in zip(*(options for _, options, _ in columns))
    ]
    kb_rows.append([
        InlineKeyboardButton(i18n.t(lang, "donate_custom_button", symbol=symbol), callback_data=f"donatecustom:{ccy}")
        for ccy, _, symbol in columns
    ])
    kb = InlineKeyboardMarkup(kb_rows)

    await update.message.reply_text(i18n.t(lang, "donate_prompt"), reply_markup=kb)


async def donate_amount_chosen(update, context) -> None:
    query = update.callback_query
    amount = int(query.data.split(":", 1)[1])
    lang = await i18n.get_lang(update.effective_user.id, context)
    await query.answer()
    error = await _send_donation_invoice(query.message.chat_id, update.effective_user, context, amount, lang)
    if error:
        await context.bot.send_message(chat_id=query.message.chat_id, text=error)


async def donate_fiat_amount_chosen(update, context) -> None:
    query = update.callback_query
    _, currency, whole_amount = query.data.split(":", 2)
    cfg = FIAT_CURRENCIES[currency]
    amount = int(whole_amount) * (10 ** cfg["exp"])
    lang = await i18n.get_lang(update.effective_user.id, context)
    await query.answer()
    error = await _send_donation_invoice(query.message.chat_id, update.effective_user, context, amount, lang, currency)
    if error:
        await context.bot.send_message(chat_id=query.message.chat_id, text=error)


async def donate_custom_button_chosen(update, context) -> None:
    """Tapping a 'Custom' button asks for an amount via ForceReply; the
    actual amount is picked up by donate_custom_amount_received below,
    matched via the donate_custom_currency flag this sets in user_data."""
    query = update.callback_query
    _, currency = query.data.split(":", 1)
    lang = await i18n.get_lang(update.effective_user.id, context)
    await query.answer()
    context.user_data["donate_custom_currency"] = currency
    unit = i18n.t(lang, "stars_unit") if currency == "XTR" else FIAT_CURRENCIES[currency]["label"]
    prompt = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=i18n.t(lang, "donate_custom_ask", unit=unit),
        reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 500"),
    )
    remember_force_reply(context, prompt)


async def donate_custom_amount_received(update, context) -> None:
    """Register in a group before your bot's normal text handling (see
    track_activity for the same pattern) -- a no-op unless
    donate_custom_button_chosen just set the awaiting-amount flag, in which
    case it consumes the reply and stops it from also being treated as a
    normal message (ApplicationHandlerStop)."""
    currency = context.user_data.get("donate_custom_currency")
    if not currency:
        return
    context.user_data.pop("donate_custom_currency", None)
    context.user_data.pop(FORCE_REPLY_KEY, None)
    lang = await i18n.get_lang(update.effective_user.id, context)

    raw = (update.message.text or "").strip().replace(",", "")
    if not raw.lstrip("-").isdigit() or int(raw) <= 0:
        await update.message.reply_text(i18n.t(lang, "donate_invalid_amount_retry"))
        raise ApplicationHandlerStop

    amount, validation_error = _validate_donation_amount(int(raw), currency, lang)
    if validation_error:
        await update.message.reply_text(validation_error)
        raise ApplicationHandlerStop

    error = await _send_donation_invoice(update.effective_chat.id, update.effective_user, context, amount, lang, currency)
    if error:
        await update.message.reply_text(error)
    raise ApplicationHandlerStop


# ---- primitives for bots that ALSO have their own Stars flow (e.g. ConvertBot's
# /convert), which need to check the payload prefix themselves and only fall
# through to these for "donate:"-prefixed ones ----

async def donation_precheckout(query) -> None:
    """Caller has already confirmed query.invoice_payload starts with 'donate:'."""
    await query.answer(ok=True)


async def donation_payment_received(update, context) -> None:
    """Caller has already confirmed the payload starts with 'donate:'."""
    sp = update.message.successful_payment
    await asyncio.to_thread(
        db.update_star_transaction, sp.invoice_payload, "paid", sp.telegram_payment_charge_id
    )
    user = update.effective_user
    emit_event(
        "info", "payment",
        f"Donation received: {format_ledger_amount(sp.total_amount, sp.currency)} "
        f"from {user.id}" + (f" (@{user.username})" if user.username else ""),
    )
    lang = await i18n.get_lang(update.effective_user.id, context)
    await update.message.reply_text(i18n.t(lang, "donate_thanks", amount=sp.total_amount))


# ---- full standalone handlers, for bots (like StickerBot) whose ONLY Stars
# usage is donations -- register these two directly, no branching needed ----

async def donation_precheckout_callback(update, context) -> None:
    query = update.pre_checkout_query
    if not query.invoice_payload.startswith("donate:"):
        await query.answer(ok=False, error_message="Unknown order.")
        return
    await donation_precheckout(query)


async def donation_payment_callback(update, context) -> None:
    sp = update.message.successful_payment
    if not sp.invoice_payload.startswith("donate:"):
        return
    await donation_payment_received(update, context)


# ---------------------------------------------------------------------------
# /cancel -- getting out of whatever the bot is waiting for
# ---------------------------------------------------------------------------
# Every bot has at least one state where it has asked a question and is now
# sitting there waiting for the answer, and until this existed there was no
# single way out of them.
#
# /cancel asks before it does anything. A bot juggling a pack, a conversion
# and a donation prompt at once had one /cancel that stopped all three, and
# no way to say which one you meant -- so the command that exists to undo a
# mistake was itself the mistake, if what you wanted was to abandon the pack
# and keep the conversion. It now offers one button per waiting thing, plus
# "everything" when there is more than one and "keep going" always. Nothing
# is stopped until a button is tapped.
#
# The exception, and it is the common case, is having nothing to cancel:
# with no pending state there is nothing to ask about, so /cancel answers
# straight away exactly as it always did.
#
# Each bot builds its own /cancel out of the pieces below: cancel_items()
# for the states this file owns, cancel_question() and the *_choice helpers
# for the asking, release_force_reply() for Telegram's client-side reply
# lock, and build_cancel_text() so all of them report back in the same
# shape. Reporting *what* was stopped is the point, not decoration.

# Where the id of the last ForceReply prompt this bot sent is parked.
FORCE_REPLY_KEY = "force_reply_msg_id"


def remember_force_reply(context, message) -> None:
    """Call right after sending anything carrying a ForceReply, so /cancel
    can delete it again. Cheap, and the entry is dropped by the handler that
    consumes the reply or by release_force_reply(), whichever comes first."""
    context.user_data[FORCE_REPLY_KEY] = message.message_id


async def release_force_reply(update, context, stored_only: bool = False) -> bool:
    """Let go of a forced reply.

    ForceReply lives on the *client*, not on the server: once a bot sends a
    message carrying one, that user's reply box stays pinned to that message
    until they answer it or it stops existing. Nothing about restarting the
    bot, or even the database, releases it -- which is how a "how many Stars
    would you like to donate?" prompt can still be demanding an answer two
    days and several redeployments later. Deleting the prompt is the only
    lever a bot actually has.

    Two ways to find it, because the first stops working the moment the
    process restarts and takes user_data with it:
      1. the id remember_force_reply() stored when we sent it, and
      2. the message this /cancel is itself a reply to -- which is exactly
         what it will be, since a forced reply box is what the user was
         looking at when they typed it.

    stored_only turns (2) off, for the button path: there, the message being
    "replied to" is the bot's own "which one?" question, whose reply target
    is the user's /cancel. Deleting that would be deleting the wrong message
    entirely -- theirs. ask_cancel_choice stores the id under (1) before the
    question goes out, so nothing is lost by ignoring (2) here.

    Best-effort throughout. Telegram only lets a bot delete messages under
    48 hours old, so an old enough prompt cannot be removed at all; that is
    what the ReplyKeyboardRemove in build_cancel_text()'s caller is for, as
    it clears the reply lock client-side regardless of the prompt's age.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    ids = []
    stored = context.user_data.pop(FORCE_REPLY_KEY, None)
    if stored:
        ids.append(stored)
    replied_to = None if stored_only else getattr(update.effective_message, "reply_to_message", None)
    if replied_to is not None and replied_to.message_id not in ids:
        ids.append(replied_to.message_id)
    released = False
    for message_id in ids:
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=message_id)
            released = True
        except Exception:
            # Already gone, too old to delete, or never ours. None of those
            # are worth failing a /cancel over.
            logging.getLogger(__name__).debug(
                "Could not delete force-reply prompt %s", message_id, exc_info=True
            )
    return released


def reset_user_state(context, keep: dict | None = None) -> None:
    """user_data.clear(), minus the one key /cancel is not finished with.

    A bot whose /cancel wipes user_data has to do it *before*
    release_force_reply() gets a chance to read the prompt id out of it,
    which quietly cost StickerBot the ability to take its own donation
    prompt back down. Keeping the key here rather than making every caller
    remember the ordering is the version that stays correct."""
    preserved = {k: context.user_data[k] for k in (FORCE_REPLY_KEY,) if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(preserved)
    if keep:
        context.user_data.update(keep)


# What /cancel offers, one per thing the bot is waiting on:
#   key    -- goes in the button's callback_data, and comes back to the bot
#   label  -- how the "Cancelled:" report names it, a full phrase
#   button -- how the button names it, short enough to read on a phone
CancelItem = namedtuple("CancelItem", "key label button")

CANCEL_PICK_PREFIX = "cancelpick:"
CANCEL_PICK_ALL = "all"
CANCEL_PICK_NONE = "none"


def cancel_items(context, lang: str) -> list[CancelItem]:
    """The waiting-on-the-user states that live in this file, for the calling
    bot to offer alongside its own. Reads state; changes none of it -- that
    is cancel_shared_item's job, once the user has actually chosen."""
    items = []
    if context.user_data.get("donate_custom_currency"):
        items.append(CancelItem("donation",
                                i18n.t(lang, "cancel_item_donation"),
                                i18n.t(lang, "cancel_button_donation")))
    return items


def cancel_shared_item(context, lang: str, key: str) -> str | None:
    """Stop one of this file's states by key. Returns how to report it, or
    None if that key is not ours or was not pending after all."""
    if key == "donation" and context.user_data.pop("donate_custom_currency", None):
        return i18n.t(lang, "cancel_item_donation")
    return None


def cancel_question(lang: str, items: list[CancelItem]):
    """The "which one?" message: what is pending, and a button each.

    One button per row rather than a grid -- these are full phrases, not
    yes/no, and a two-column layout truncates them on a phone. "Everything"
    only appears when there is more than one thing it could mean, and "keep
    going" always does, because opening this menu must not be a one-way
    door: /cancel is what people reach for when they are already unsure.
    """
    text = i18n.t(lang, "cancel_ask") + "\n" + "\n".join(
        f"\u2022 {item.label}" for item in items
    )
    rows = [[InlineKeyboardButton(item.button, callback_data=CANCEL_PICK_PREFIX + item.key)]
            for item in items]
    if len(items) > 1:
        rows.append([InlineKeyboardButton(i18n.t(lang, "cancel_button_all"),
                                          callback_data=CANCEL_PICK_PREFIX + CANCEL_PICK_ALL)])
    rows.append([InlineKeyboardButton(i18n.t(lang, "cancel_button_none"),
                                      callback_data=CANCEL_PICK_PREFIX + CANCEL_PICK_NONE)])
    return text, InlineKeyboardMarkup(rows)


async def ask_cancel_choice(update, context, items: list[CancelItem], lang: str) -> bool:
    """Put the question on screen. False means there was nothing to ask
    about, and the caller should answer the old way -- see finish_cancel.

    Remembers what /cancel was a reply to, because the answer arrives as a
    button tap on a different message and release_force_reply would have no
    way to find the prompt otherwise.
    """
    if not items:
        return False
    replied_to = getattr(update.effective_message, "reply_to_message", None)
    if replied_to is not None and FORCE_REPLY_KEY not in context.user_data:
        context.user_data[FORCE_REPLY_KEY] = replied_to.message_id
    text, keyboard = cancel_question(lang, items)
    await update.effective_message.reply_text(text, reply_markup=keyboard)
    return True


def cancel_choice_key(update) -> str:
    """The key behind the tapped button -- an item's own, or "all"/"none"."""
    return update.callback_query.data.split(":", 1)[1]


def build_cancel_text(lang: str, stopped: list[str]) -> str:
    if not stopped:
        return i18n.t(lang, "cancel_nothing")
    return i18n.t(lang, "cancel_header") + "\n" + "\n".join(f"\u2022 {item}" for item in stopped)


async def finish_cancel(update, context, lang: str, stopped: list[str]) -> None:
    """The last two steps of every bot's /cancel: release the reply lock and
    say what was stopped. ReplyKeyboardRemove is what actually unpins the
    reply box on the client when the prompt itself was too old to delete."""
    released = await release_force_reply(update, context)
    # A prompt we could still delete, but nothing in memory to go with it, is
    # the signature of one that outlived the process that sent it: user_data
    # does not survive a restart, but Telegram's reply lock does. Saying
    # "nothing to cancel" while visibly deleting the thing that was pestering
    # them is the one answer that would make no sense here.
    if released and not stopped:
        stopped = [i18n.t(lang, "cancel_item_stale_prompt")]
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        build_cancel_text(lang, stopped),
        reply_markup=ReplyKeyboardRemove(),
    )


async def finish_cancel_choice(update, context, lang: str, stopped: list[str]) -> None:
    """finish_cancel's twin for the button path.

    The question message becomes the report, which is both tidier than a
    reply underneath it and the only way to make sure a set of buttons that
    has already been acted on cannot be tapped a second time.

    An edit cannot carry ReplyKeyboardRemove, so the client-side reply lock
    gets its own one-line message -- but only in the case that actually
    needs it: a prompt we knew about and could not delete, which means it is
    over Telegram's 48-hour deletion limit and still pinning the reply box.
    A prompt old enough for that has almost always outlived the process that
    sent it, in which case nothing is pending, nothing was asked, and this
    path was never reached at all.
    """
    query = update.callback_query
    had_prompt = FORCE_REPLY_KEY in context.user_data
    released = await release_force_reply(update, context, stored_only=True)
    if released and not stopped:
        stopped = [i18n.t(lang, "cancel_item_stale_prompt")]
    text = build_cancel_text(lang, stopped)
    try:
        await query.message.edit_text(text)
    except Exception:
        logging.getLogger(__name__).debug("Could not edit the cancel menu", exc_info=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
    if had_prompt and not released:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=i18n.t(lang, "cancel_reply_box_freed"),
            reply_markup=ReplyKeyboardRemove(),
        )


async def keep_going(update, context, lang: str) -> None:
    """"Keep going" -- the way back out of the question, having changed
    nothing. The menu is replaced rather than left sitting there with live
    buttons under an answer that has already been given."""
    try:
        await update.callback_query.message.edit_text(i18n.t(lang, "cancel_kept"))
    except Exception:
        logging.getLogger(__name__).debug("Could not edit the cancel menu", exc_info=True)


# ---------------------------------------------------------------------------
# Logging, crash tracking, active-user tracking, hosting detection --
# all in support of each bot's owner-only /status command.
# ---------------------------------------------------------------------------
# Each bot's own process gets its own logs/ folder (next to its bot.py) --
# same "no shared files between bots" independence as everything else here.
# bot.log gets everything at INFO+; errors.log gets WARNING+ only, from any
# logger in the process (not just the PTB error handler below), so a stray
# warning logged deep in some helper module still ends up somewhere findable
# without needing to grep the full bot.log.

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

# In-memory only -- resets on restart, which conveniently lines up with
# "since this deployment" rather than needing its own DB table. Capped at 10
# so a crash loop can't blow up memory; the running _error_count below still
# reflects the true total even once older entries fall off the deque.
_recent_errors: deque[tuple[str, str]] = deque(maxlen=10)
_error_count = 0

# Set by family_link.attach() to family_link.report_event_soon, so anything
# worth waking the owner up for also reaches ParentBot. Left as None when a
# bot runs standalone (FAMILY_BUS=off, or no shared database reachable) --
# every call site below tolerates that, and none of them may ever raise:
# the most important caller is record_error(), i.e. the crash path itself.
_event_hook = None


def set_event_hook(fn) -> None:
    global _event_hook
    _event_hook = fn


def emit_event(level: str, kind: str, message: str, details: str | None = None) -> None:
    if _event_hook is None:
        return
    try:
        _event_hook(level, kind, message, details)
    except Exception:
        logging.getLogger(__name__).debug("Family event hook failed", exc_info=True)


def _log_to_files() -> bool:
    """Files on a laptop, stdout only in the cloud.

    On a host like Railway the container's filesystem is ephemeral and the
    platform's own log viewer already captures stdout, so the rotating files
    are three handlers' worth of formatting plus up to 12 MB of disk writes
    that nobody will ever read -- and disk writes are the slowest thing a
    small container does. LOG_TO_FILES=1 forces them back on anywhere;
    LOG_TO_FILES=0 forces them off locally."""
    override = os.environ.get("LOG_TO_FILES")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    return not (os.environ.get("RAILWAY_ENVIRONMENT_NAME") or os.environ.get("RAILWAY_ENVIRONMENT"))


def setup_logging(bot_file: str) -> None:
    """Call once near the top of each bot's bot.py, passing __file__.

    Always logs to the console -- that is what `docker logs` and Railway's
    log viewer show. Adds rotating bot.log/errors.log files next to that
    file when running somewhere they will actually survive (see
    _log_to_files above)."""
    fmt = logging.Formatter(_LOG_FORMAT)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # These three are chatty at INFO -- httpx logs a line per HTTP request,
    # which for a long-polling bot means one every few seconds, forever, for
    # no information at all. Warnings and above still come through.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not _log_to_files():
        return

    log_dir = os.path.join(os.path.dirname(os.path.abspath(bot_file)), "logs")
    os.makedirs(log_dir, exist_ok=True)

    info_file = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    info_file.setFormatter(fmt)
    root.addHandler(info_file)

    error_file = RotatingFileHandler(
        os.path.join(log_dir, "errors.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    error_file.setLevel(logging.WARNING)
    error_file.setFormatter(fmt)
    root.addHandler(error_file)


# ---------------------------------------------------------------------------
# Transient network conditions vs. actual faults
# ---------------------------------------------------------------------------
# A long poll losing its connection, a read timing out, Telegram asking us to
# slow down: PTB retries all of these itself and the bot keeps working, so
# waking the owner with a traceback for each one is noise that trains you to
# ignore the channel that also carries real crashes.
#
# The catch is PTB's class hierarchy: BadRequest *subclasses* NetworkError,
# and a BadRequest is a genuine fault -- a malformed API call, our bug -- so a
# plain `isinstance(exc, NetworkError)` would swallow exactly the errors most
# worth hearing about. It has to be excluded explicitly.

TRANSIENT_NETWORK_ERRORS = (NetworkError, RetryAfter)

# How many blips in a row before saying something. Reset by any update that
# arrives, since one arriving proves the connection is working again. Roughly
# a few minutes of a dead link at a 30-second poll.
NETWORK_ALERT_AFTER = int(os.environ.get("NETWORK_ALERT_AFTER", "20"))

_network_blips = 0        # consecutive, since the last update actually arrived
_network_blips_total = 0  # since this process started
_network_alerted = False


def is_transient_network_error(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_NETWORK_ERRORS) and not isinstance(exc, BadRequest)


def note_network_blip(exc: BaseException) -> None:
    """Counted and logged, never reported as a crash -- until there have been
    enough in a row to mean the connection is gone rather than flaky, which is
    worth exactly one message."""
    global _network_blips, _network_blips_total, _network_alerted
    _network_blips += 1
    _network_blips_total += 1
    logging.getLogger(__name__).warning(
        "Transient network error (%s): %s -- retried by PTB, %s in a row",
        type(exc).__name__, exc, _network_blips,
    )
    if _network_blips >= NETWORK_ALERT_AFTER and not _network_alerted:
        _network_alerted = True
        emit_event(
            "warning", "network",
            f"{_network_blips} network errors in a row -- this bot may not be "
            f"reaching Telegram. Latest: {type(exc).__name__}: {exc}",
        )


def note_network_ok() -> None:
    """An update arrived, so the connection works. Called from track_activity,
    which runs before every other handler."""
    global _network_blips, _network_alerted
    if _network_blips and _network_alerted:
        emit_event("info", "network", "Telegram is reachable again.")
    _network_blips = 0
    _network_alerted = False


def record_error(exc: BaseException) -> None:
    global _error_count
    _error_count += 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _recent_errors.append((stamp, repr(exc)))
    emit_event(
        "error", "crash", f"Unhandled {type(exc).__name__}: {exc}",
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )


def error_summary() -> str:
    blips = (
        f"\n\U0001f310 {_network_blips_total} transient network error(s) -- retried, not crashes."
        if _network_blips_total else ""
    )
    if _error_count == 0:
        return "✅ No errors since this instance started." + blips
    lines = [f"⚠️ {_error_count} error(s) since start:"]
    lines.extend(f"  {stamp} — {msg}" for stamp, msg in _recent_errors)
    if _error_count > len(_recent_errors):
        lines.append(f"  (+{_error_count - len(_recent_errors)} earlier, see logs/errors.log)")
    return "\n".join(lines) + blips


async def error_handler(update, context) -> None:
    """Register with Application.add_error_handler in each bot's main() --
    this is PTB's global hook for exceptions that escape a handler callback
    uncaught (i.e. actual crashes, not the try/except'd, user-facing errors
    already handled inline elsewhere).

    A dropped long poll reaches here too, and is not a crash -- see
    is_transient_network_error above."""
    if is_transient_network_error(context.error):
        note_network_blip(context.error)
        return
    logging.getLogger(__name__).error("Unhandled exception while processing an update", exc_info=context.error)
    record_error(context.error)


# ---------------------------------------------------------------------------
# Active-user tracking, buffered
# ---------------------------------------------------------------------------
# This used to be one INSERT -- and, before the connection pool, one whole new
# Postgres connection -- on *every single update*, purely so /status could say
# how many people used the bot in the last hour.
#
# Nothing reads an individual row. Both queries are COUNT(DISTINCT user_id)
# over a window, so recording a user once per flush window is exactly as
# accurate as recording them forty times, and writes a fraction as much. Ids
# collect in a set here and go out as one multi-row INSERT per window.
ACTIVITY_FLUSH_SECONDS = int(os.environ.get("ACTIVITY_FLUSH_SECONDS", "60"))

_activity_buffer: set[int] = set()


def _flush_activity_now() -> int:
    """Blocking; call through asyncio.to_thread. Takes the whole buffer in one
    swap so an update arriving mid-flush lands in the next window rather than
    being lost."""
    global _activity_buffer
    if not _activity_buffer:
        return 0
    batch, _activity_buffer = _activity_buffer, set()
    try:
        db.record_activity_batch(batch)
    except Exception:
        # Put them back: a database blip should cost a delayed count, not a
        # wrong one. Union rather than assignment, so ids that arrived while
        # this was in flight survive too.
        _activity_buffer |= batch
        raise
    return len(batch)


async def _flush_activity_job(context) -> None:
    try:
        await asyncio.to_thread(_flush_activity_now)
    except Exception:
        logging.getLogger(__name__).debug("Activity flush failed; will retry", exc_info=True)


async def track_activity(update, context) -> None:
    """Register as a TypeHandler(Update, track_activity, ...) in group=-1
    (runs before every other handler, but doesn't stop them) so /status can
    report active users hourly / since this process started.

    Does no I/O at all now -- it adds an int to a set, and the job registered
    by attach_maintenance() writes the window out. It also stamps this user's
    per-user cache with the time, which is what lets that same job drop the
    caches of people who have not been seen in a long while."""
    note_network_ok()
    user = update.effective_user
    if not user:
        return
    _activity_buffer.add(user.id)
    context.user_data["_last_seen"] = time.time()


# ---------------------------------------------------------------------------
# Bounding the process's own memory
# ---------------------------------------------------------------------------
# python-telegram-bot keeps a user_data dict per user id for the lifetime of
# the process and never evicts it. Each one is small (a cached language, a
# menu's worth of state) but the count only ever goes up, so on a bot that
# runs for months this is a genuine slow leak -- and on a container sized to
# the smallest plan that fits, a slow leak is an eventual OOM restart.
#
# Anyone idle for USER_DATA_TTL_HOURS gets theirs dropped. The only thing lost
# is a cache: their language is re-read from the database on their next
# message, and no bot in the family keeps anything in user_data that has to
# outlive a conversation.
USER_DATA_TTL_HOURS = int(os.environ.get("USER_DATA_TTL_HOURS", "12"))


def _prune_user_data(application) -> int:
    cutoff = time.time() - USER_DATA_TTL_HOURS * 3600
    stale = [
        user_id for user_id, data in application.user_data.items()
        if data.get("_last_seen", 0) < cutoff
    ]
    for user_id in stale:
        application.drop_user_data(user_id)
    return len(stale)


async def _maintenance_job(context) -> None:
    dropped = _prune_user_data(context.application)
    if dropped:
        logging.getLogger(__name__).info("Dropped cached state for %d idle user(s).", dropped)


def attach_maintenance(app) -> None:
    """One line in each bot's main(), next to family_link.attach().

    Flushes the activity buffer on a timer and again on shutdown, and keeps
    per-user memory from growing forever. Degrades to "no buffering, no
    pruning" rather than failing if the bot has no job queue."""
    if app.job_queue is None:
        logging.getLogger(__name__).warning(
            "No job queue -- activity counts and memory pruning are off. "
            'Install it with: pip install "python-telegram-bot[job-queue]"'
        )
        return
    app.job_queue.run_repeating(
        _flush_activity_job, interval=ACTIVITY_FLUSH_SECONDS, first=ACTIVITY_FLUSH_SECONDS
    )
    app.job_queue.run_repeating(_maintenance_job, interval=3600, first=3600)


async def flush_on_shutdown(application) -> None:
    """Register as Application.post_stop. Writes out whatever the last window
    collected -- without this, a redeploy silently loses up to a minute of
    activity counts -- and lets the connection pool's worker threads go."""
    try:
        await asyncio.to_thread(_flush_activity_now)
    except Exception:
        logging.getLogger(__name__).debug("Final activity flush failed", exc_info=True)
    close = getattr(db, "close_pool", None)
    if close is not None:
        try:
            await asyncio.to_thread(close)
        except Exception:
            logging.getLogger(__name__).debug("Closing the connection pool failed", exc_info=True)


def detect_host_environment() -> str:
    """Best-effort guess at where this process is running. Railway sets a
    handful of its own env vars on every deploy, so their presence is a
    reliable cloud signal; anything else is assumed to be a local machine."""
    railway_env = os.environ.get("RAILWAY_ENVIRONMENT_NAME") or os.environ.get("RAILWAY_ENVIRONMENT")
    if railway_env:
        project = os.environ.get("RAILWAY_PROJECT_NAME", "?")
        service = os.environ.get("RAILWAY_SERVICE_NAME", "?")
        return f"☁️ Cloud (Railway -- project \"{project}\", service \"{service}\", env \"{railway_env}\")"
    return f"💻 Local ({socket.gethostname()})"


def build_status_text(start_time: datetime, users_last_hour: int, users_since_start: int) -> str:
    now = datetime.now(timezone.utc)
    uptime = now - start_time
    days, rem = divmod(int(uptime.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
    return "\n".join([
        "📊 Status",
        f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')} ({uptime_str} ago)",
        f"Hosted: {detect_host_environment()}",
        f"Active users (last hour): {users_last_hour}",
        f"Active users (since this start): {users_since_start}",
        "",
        error_summary(),
    ])
