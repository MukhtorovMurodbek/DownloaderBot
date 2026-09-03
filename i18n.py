"""Translation strings for DownloaderBot's end-user-facing text (English,
Uzbek, Russian). Deliberately duplicated per bot -- same "no shared files
between bots" independence as shared_features.py -- but the STRINGS content
here is specific to this bot's own commands and flows.

Admin-only output (/dbdump, /status) is intentionally NOT translated -- see
build_status_text/error_summary/detect_host_environment in
shared_features.py, left as plain English since only the bot owner reads
them. Text baked into rendered card images (cards.py) is also left in
English -- Pillow's built-in bitmap font (ImageFont.load_default) has no
Cyrillic glyphs, so translating it there would just draw tofu boxes.

The keys below split into two groups:
  - "Shared" keys (donate flow, sibling-bot blurb) exist under the exact
    same names in every bot's i18n.py, since shared_features.py is
    duplicated byte-identical across the family and calls t() with these
    names regardless of which bot it's running in.
  - Bot-specific keys, everything below the shared block, for this bot's
    own bot.py strings only.
"""
import asyncio

import db

SUPPORTED_LANGUAGES = ("en", "uz", "ru")
LANGUAGE_LABELS = {"en": "English 🇬🇧", "uz": "O'zbekcha 🇺🇿", "ru": "Русский 🇷🇺"}

# What every /start shows, whether or not the user already has a language.
# Deliberately not part of STRINGS: it is trilingual on purpose, so there
# is no single `lang` to look it up under.
LANGUAGE_PROMPT = (
    "👋 Welcome! / Xush kelibsiz! / Добро пожаловать!\n\n"
    "Please choose your language / Iltimos, tilni tanlang / "
    "Пожалуйста, выберите язык:"
)

STRINGS = {
    "en": {
        # ---- shared keys (same name in every bot's i18n.py) ----
        "sibling_blurb": "Also part of this bot family, see below \U0001f447",
        "donation_nudge": (
            "💙 If this bot's been useful: hosting/API costs are covered by whoever's "
            "running it, and /donate is a totally optional way to help keep it alive. "
            "No pressure either way!"
        ),
        "donate_unknown_currency": 'Unknown currency "{currency}" -- try xtr or usd.',
        "donate_currency_not_configured": "{currency} donations aren't set up on this bot yet -- try Stars instead.",
        "donate_invalid_amount": "That's not a valid amount -- try e.g. /donate 500 or /donate 5 usd.",
        "donate_prompt": (
            "Thank you for contributing -- it goes directly toward this bot's "
            "hosting and API costs. Choose an amount below, or Custom to enter "
            "your own (you can also send /donate <number> [usd] directly)."
        ),
        "donate_custom_button": "✏️ Custom {symbol}",
        "donate_too_many_stars": "That's a lot of stars! Keep it under {max} ⭐ per donation.",
        "donate_out_of_range": "{currency} donations need to be between {lo} and {hi} {symbol}.",
        "donate_invoice_title": "Buy the bot a coffee ☕",
        "donate_invoice_description": "A one-time voluntary donation towards hosting costs. Thank you!",
        "donate_invoice_label": "Donation",
        "donate_invoice_error": "⚠️ Telegram wouldn't create that invoice: {error}",
        "stars_unit": "Stars",
        "donate_custom_ask": "How many {unit} would you like to donate? Reply with a number.",
        "donate_invalid_amount_retry": "That's not a valid amount -- send /donate to try again.",
        "donate_thanks": "🙏 Thank you for the {amount} ⭐ — genuinely appreciated!",
        "language_set_confirmation": "✅ Language set to English.",
        "cancel_header": "\u274c Cancelled:",
        "cancel_nothing": "Nothing to cancel -- I wasn't waiting on anything from you.",
        "cancel_ask": "What should I stop? Here's what I'm waiting on:",
        "cancel_kept": "Alright -- nothing cancelled.",
        "cancel_reply_box_freed": "Your reply box is free again.",
        "cancel_button_all": "❌ All of it",
        "cancel_button_none": "↩️ Nothing, keep going",
        "cancel_button_donation": "💸 Donation amount",
        "cancel_item_donation": "the donation amount I asked you for",
        "cancel_item_stale_prompt": "a leftover prompt that was still waiting on an answer",
        # ---- downloader_bot-specific keys ----
        "start_greeting": (
            "Hey! Send me a link (Instagram, TikTok, Pinterest, Reddit, or "
            "Twitter/X) and I'll grab it for you.\n\n"
        ),
        "help_text": (
            "Paste a link any time and I'll grab it -- no command needed:\n"
            "  - Instagram / TikTok: video\n"
            "  - Pinterest: the pin's image (or video, if it's a video pin)\n"
            "  - Reddit: the media if it's a media post, or a clean image card of "
            "the post if it's text/a link\n"
            "  - Twitter/X: same split as Reddit -- media, or a card for text tweets\n\n"
            "Commands:\n"
            '/caption on|off - toggle the "via @{username}" credit caption\n'
            "/lossless on|off - get downloads as uncompressed files\n"
            "/donate - chip in for hosting costs (totally optional)\n"
            "/cancel - stop something I'm waiting on you for (I'll ask which)\n"
            "/en, /uz, /rus - switch language (or /language, which asks)\n\n"
            "This bot is still being developed and hosted temporarily -- if it's not "
            "responding, it should come back the next time I'm running it.\n\n"
        ),
        "caption_state_on": "ON",
        "caption_state_off": "OFF",
        "caption_status": "Caption is currently {state}.",
        "caption_turned": "Download caption turned {state}.",
        "caption_toggle_answer": "Caption turned {state}.",
        "lossless_state_on": "ON",
        "lossless_state_off": "OFF",
        "lossless_status": "Lossless is currently {state}.\n\nON: downloads arrive as files, exactly as the source had them -- no Telegram re-compression, but they don't play or preview in the chat until you open them.\nOFF: downloads arrive as photos and videos that play inline, compressed by Telegram.",
        "lossless_turned": "Lossless downloads turned {state}.",
        "lossless_toggle_answer": "Lossless turned {state}.",
        "download_credit_caption": "⬇️ via @{username}",
        "downloading": "Downloading...",
        "queued": "⏳ Busy with another download — yours starts in a moment.",
        "restarting_send_again": "🔄 I'm being updated right now — give me a few seconds and send that again.",
        "update_soon_try_later": "🔧 I'm being updated in a moment, so I can't start anything new right now — please try again in about {minutes} minute(s). I'll message you when I'm back.",
        "update_soon_try_later_soon": "🔧 I'm being updated right now, so I can't start anything new — please try again shortly. I'll message you when I'm back.",
        "update_will_reset": "🔧 Heads up: I'm about to be updated, and what you have going right now will be reset. You'll be able to start it again in a few minutes.",
        "update_done_try_now": '✅ The update is done — go ahead and try again now.',
        "download_failed": "Couldn't download that: {error}",
        "source_blocked_server": "🚫 YouTube is asking this server to prove it isn't a bot — something it does to servers, not to people. Nothing is wrong with your link. Try again in a while, or send it to me from a different site if you can.",
        "source_needs_login": "🔒 That one is behind a login. Instagram stopped serving posts to servers that aren't signed in, so I can't reach it from here — your link is fine, and trying again won't change it. Public posts from other sites still work.",
        "fetching": "Fetching...",
        "reddit_fetch_failed": "Couldn't fetch that Reddit post: {error}",
        "twitter_fetch_failed_link": "Couldn't fetch that post's content right now -- here's the link: {url}",
        "unrecognized_message": (
            "That doesn't look like a link I recognize -- Instagram, TikTok, "
            "Pinterest, Reddit, or Twitter/X -- paste one to download it, or "
            "/help for commands."
        ),
        "unknown_command": "I don't recognize that command. Send /help to see what I can do.",
    },
    "uz": {
        "sibling_blurb": "Bu bot oilasining bir qismi, pastda ko'ring \U0001f447",
        "donation_nudge": (
            "💙 Agar bu bot foydali bo'lgan bo'lsa: hosting/API xarajatlarini uni ishga "
            "tushirgan kishi qoplaydi, /donate esa uni tirik saqlashga yordam berishning "
            "ixtiyoriy usuli. Bosim yo'q, xohlasangiz ham, xohlamasangiz ham!"
        ),
        "donate_unknown_currency": '"{currency}" — noma\'lum valyuta. xtr yoki usd dan foydalaning.',
        "donate_currency_not_configured": "{currency} orqali xayriya bu botda hali sozlanmagan — Stars dan foydalaning.",
        "donate_invalid_amount": "Bu noto'g'ri miqdor — masalan, /donate 500 yoki /donate 5 usd deb yozing.",
        "donate_prompt": (
            "Hissa qo'shganingiz uchun rahmat — bu mablag' to'g'ridan-to'g'ri "
            "botning hosting va API xarajatlariga sarflanadi. Quyidan miqdorni "
            "tanlang yoki o'zingiz kiritish uchun \"Boshqa\"ni bosing (shuningdek, "
            "to'g'ridan-to'g'ri /donate <son> [usd] deb yuborishingiz mumkin)."
        ),
        "donate_custom_button": "✏️ Boshqa {symbol}",
        "donate_too_many_stars": "Bu juda ko'p yulduzcha! Har bir xayriya {max} ⭐ dan kam bo'lsin.",
        "donate_out_of_range": "{currency} xayriyalar {lo} va {hi} {symbol} oralig'ida bo'lishi kerak.",
        "donate_invoice_title": "Botga bir chashka qahva sotib oling ☕",
        "donate_invoice_description": "Hosting xarajatlariga bir martalik ixtiyoriy xayriya. Rahmat!",
        "donate_invoice_label": "Xayriya",
        "donate_invoice_error": "⚠️ Telegram bu hisob-fakturani yarata olmadi: {error}",
        "stars_unit": "Stars (yulduzcha)",
        "donate_custom_ask": "Nechta {unit} xayriya qilmoqchisiz? Raqam bilan javob bering.",
        "donate_invalid_amount_retry": "Bu noto'g'ri miqdor — qayta urinish uchun /donate yuboring.",
        "donate_thanks": "🙏 {amount} ⭐ uchun rahmat — bu chindan ham qadrlanadi!",
        "language_set_confirmation": "✅ Til o'zbekchaga o'zgartirildi.",
        "cancel_header": "\u274c Bekor qilindi:",
        "cancel_nothing": "Bekor qiladigan narsa yo'q -- men sizdan hech narsa kutmayotgan edim.",
        "cancel_ask": "Nimani to'xtatay? Mana, men nimalarni kutyapman:",
        "cancel_kept": "Yaxshi -- hech narsa bekor qilinmadi.",
        "cancel_reply_box_freed": "Javob yozish oynasi yana bo'sh.",
        "cancel_button_all": "❌ Hammasini",
        "cancel_button_none": "↩️ Hech narsani, davom etamiz",
        "cancel_button_donation": "💸 Xayriya miqdori",
        "cancel_item_donation": "men so'ragan xayriya miqdori",
        "cancel_item_stale_prompt": "javob kutib qolgan eski so'rov",
        "start_greeting": (
            "Salom! Menga havola yuboring (Instagram, TikTok, Pinterest, Reddit "
            "yoki Twitter/X) va men uni siz uchun yuklab beraman.\n\n"
        ),
        "help_text": (
            "Istalgan vaqtda havola yuboring — men uni olib beraman, buyruq shart emas:\n"
            "  - Instagram / TikTok: video\n"
            "  - Pinterest: pin rasmi (agar video-pin bo'lsa — video)\n"
            "  - Reddit: agar media post bo'lsa — media, matn/havola bo'lsa — toza rasm-karta\n"
            "  - Twitter/X: Reddit bilan bir xil — media yoki matnli tvitlar uchun karta\n\n"
            "Buyruqlar:\n"
            '/caption on|off - yuklamalardagi "via @{username}" yozuvini yoqish/o\'chirish\n'
            "/lossless on|off - yuklamalarni siqilmagan fayl sifatida olish\n"
            "/donate - hosting xarajatlariga hissa qo'shish (ixtiyoriy)\n"
            "/cancel - men sizdan kutayotgan ishni to'xtatish (qaysinisini so'rayman)\n"
            "/en, /uz, /rus - tilni almashtirish (yoki /language — u so\'raydi)\n\n"
            "Bu bot hali ishlab chiqilmoqda va vaqtinchalik joylashtirilgan — agar "
            "javob bermasa, keyingi safar ishga tushirilganda qaytadi.\n\n"
        ),
        "caption_state_on": "YONIQ",
        "caption_state_off": "O'CHIQ",
        "caption_status": "Izoh hozir {state}.",
        "caption_turned": "Yuklama izohi {state} qilindi.",
        "caption_toggle_answer": "Izoh {state} qilindi.",
        "lossless_state_on": "YONIQ",
        "lossless_state_off": "O'CHIQ",
        "lossless_status": "Yo'qotishsiz rejim hozir {state}.\n\nYONIQ: yuklamalar fayl sifatida keladi, manbadagidek aynan -- Telegram siqmaydi, lekin ochmaguningizcha chatda ko'rinmaydi va ijro etilmaydi.\nO'CHIQ: yuklamalar chatda darhol ijro etiladigan rasm va video sifatida, Telegram siqishi bilan keladi.",
        "lossless_turned": "Yo'qotishsiz yuklash {state} qilindi.",
        "lossless_toggle_answer": "Yo'qotishsiz rejim {state} qilindi.",
        "download_credit_caption": "⬇️ @{username} orqali",
        "downloading": "Yuklanmoqda...",
        "queued": "⏳ Boshqa yuklab olish bilan bandman — sizniki hozir boshlanadi.",
        "restarting_send_again": "🔄 Hozir yangilanmoqdaman — bir necha soniyadan so'ng buni qaytadan yuboring.",
        "update_soon_try_later": "🔧 Hozir yangilanaman, shuning uchun yangi ish boshlay olmayman — taxminan {minutes} daqiqadan so'ng qaytadan urinib ko'ring. Qaytganimda o'zim xabar beraman.",
        "update_soon_try_later_soon": "🔧 Hozir yangilanmoqdaman, shuning uchun yangi ish boshlay olmayman — birozdan so'ng qaytadan urinib ko'ring. Qaytganimda o'zim xabar beraman.",
        "update_will_reset": "🔧 Diqqat: men yangilanmoqchiman va hozir boshlagan ishingiz bekor qilinadi. Bir necha daqiqadan so'ng qaytadan boshlashingiz mumkin.",
        "update_done_try_now": "✅ Yangilanish tugadi — endi qaytadan urinib ko'rishingiz mumkin.",
        "download_failed": "Buni yuklab bo'lmadi: {error}",
        "source_blocked_server": '🚫 YouTube bu serverdan “bot emasligini” tasdiqlashni so‘rayapti — buni odamlardan emas, serverlardan so‘raydi. Havolangizda xatolik yo‘q. Birozdan so‘ng qaytadan urinib ko‘ring.',
        "source_needs_login": '🔒 Bu post tizimga kirishni talab qiladi. Instagram tizimga kirmagan serverlarga postlarni bermay qo‘ydi, shuning uchun bu yerdan unga yeta olmayman — havolangiz joyida, qayta urinish ham yordam bermaydi. Boshqa saytlardagi ochiq postlar ishlashda davom etadi.',
        "fetching": "Olinmoqda...",
        "reddit_fetch_failed": "Bu Reddit postini olib bo'lmadi: {error}",
        "twitter_fetch_failed_link": "Hozircha bu post mazmunini olib bo'lmadi — mana havola: {url}",
        "unrecognized_message": (
            "Bu men taniydigan havolaga o'xshamayapti — Instagram, TikTok, "
            "Pinterest, Reddit yoki Twitter/X — yuklab olish uchun shulardan "
            "birini yuboring yoki buyruqlar uchun /help ni bosing."
        ),
        "unknown_command": "Bu buyruqni tanimadim. Nima qila olishimni bilish uchun /help yuboring.",
    },
    "ru": {
        "sibling_blurb": "Тоже часть этой семьи ботов, смотри ниже \U0001f447",
        "donation_nudge": (
            "💙 Если этот бот оказался полезным: расходы на хостинг/API покрывает тот, "
            "кто его запустил, а /donate — это совершенно необязательный способ помочь "
            "ему остаться на плаву. Никакого давления в любом случае!"
        ),
        "donate_unknown_currency": 'Неизвестная валюта "{currency}" — попробуйте xtr или usd.',
        "donate_currency_not_configured": "Пожертвования в {currency} на этом боте пока не настроены — попробуйте Stars.",
        "donate_invalid_amount": "Это некорректная сумма — попробуйте, например, /donate 500 или /donate 5 usd.",
        "donate_prompt": (
            "Спасибо за вклад — эти средства идут прямо на хостинг и API этого "
            "бота. Выберите сумму ниже или нажмите «Другое», чтобы ввести свою "
            "(также можно сразу отправить /donate <число> [usd])."
        ),
        "donate_custom_button": "✏️ Другое {symbol}",
        "donate_too_many_stars": "Это очень много звёзд! Пусть будет меньше {max} ⭐ за одно пожертвование.",
        "donate_out_of_range": "Пожертвования в {currency} должны быть в диапазоне от {lo} до {hi} {symbol}.",
        "donate_invoice_title": "Угостите бота кофе ☕",
        "donate_invoice_description": "Разовое добровольное пожертвование на хостинг. Спасибо!",
        "donate_invoice_label": "Пожертвование",
        "donate_invoice_error": "⚠️ Telegram не смог создать этот счёт: {error}",
        "stars_unit": "Stars (звёзды)",
        "donate_custom_ask": "Сколько {unit} вы хотите пожертвовать? Ответьте числом.",
        "donate_invalid_amount_retry": "Это некорректная сумма — отправьте /donate, чтобы попробовать снова.",
        "donate_thanks": "🙏 Спасибо за {amount} ⭐ — это по-настоящему ценно!",
        "language_set_confirmation": "✅ Язык изменён на русский.",
        "cancel_header": "\u274c \u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e:",
        "cancel_nothing": "\u041e\u0442\u043c\u0435\u043d\u044f\u0442\u044c \u043d\u0435\u0447\u0435\u0433\u043e -- \u044f \u043d\u0438\u0447\u0435\u0433\u043e \u043e\u0442 \u0432\u0430\u0441 \u043d\u0435 \u0436\u0434\u0430\u043b.",
        "cancel_ask": "Что остановить? Вот что я жду:",
        "cancel_kept": "Хорошо -- ничего не отменено.",
        "cancel_reply_box_freed": "Поле ответа снова свободно.",
        "cancel_button_all": "❌ Всё",
        "cancel_button_none": "↩️ Ничего, продолжаем",
        "cancel_button_donation": "💸 Сумма пожертвования",
        "cancel_item_donation": "\u0441\u0443\u043c\u043c\u0430 \u043f\u043e\u0436\u0435\u0440\u0442\u0432\u043e\u0432\u0430\u043d\u0438\u044f, \u043a\u043e\u0442\u043e\u0440\u0443\u044e \u044f \u0437\u0430\u043f\u0440\u043e\u0441\u0438\u043b",
        "cancel_item_stale_prompt": "старый запрос, который всё ещё ждал ответа",
        "start_greeting": (
            "Привет! Пришли мне ссылку (Instagram, TikTok, Pinterest, Reddit "
            "или Twitter/X), и я скачаю это для тебя.\n\n"
        ),
        "help_text": (
            "Просто пришли ссылку в любое время — я её заберу, команда не нужна:\n"
            "  - Instagram / TikTok: видео\n"
            "  - Pinterest: изображение пина (или видео, если это видео-пин)\n"
            "  - Reddit: медиа, если это медиа-пост, или аккуратная карточка-изображение, "
            "если это текст/ссылка\n"
            "  - Twitter/X: то же самое, что и Reddit — медиа или карточка для текстовых твитов\n\n"
            "Команды:\n"
            '/caption on|off - включить/выключить подпись "via @{username}" на загрузках\n'
            "/lossless on|off - получать загрузки несжатыми файлами\n"
            "/donate - помочь с расходами на хостинг (совершенно необязательно)\n"
            "/cancel - остановить то, чего я от вас жду (спрошу, что именно)\n"
            "/en, /uz, /rus - сменить язык (или /language — он спрашивает)\n\n"
            "Этот бот всё ещё находится в разработке и размещён временно — если он не "
            "отвечает, он должен вернуться в следующий раз, когда я его запущу.\n\n"
        ),
        "caption_state_on": "ВКЛ",
        "caption_state_off": "ВЫКЛ",
        "caption_status": "Подпись сейчас {state}.",
        "caption_turned": "Подпись к загрузкам теперь {state}.",
        "caption_toggle_answer": "Подпись теперь {state}.",
        "lossless_state_on": "ВКЛ",
        "lossless_state_off": "ВЫКЛ",
        "lossless_status": "Режим без потерь сейчас {state}.\n\nВКЛ: загрузки приходят файлом, ровно такими, какими были в источнике -- Telegram их не пережимает, но они не проигрываются в чате, пока вы их не откроете.\nВЫКЛ: загрузки приходят фото и видео, которые играют прямо в чате, со сжатием Telegram.",
        "lossless_turned": "Загрузка без потерь теперь {state}.",
        "lossless_toggle_answer": "Режим без потерь теперь {state}.",
        "download_credit_caption": "⬇️ через @{username}",
        "downloading": "Скачивание...",
        "queued": "⏳ Занят другой загрузкой — ваша начнётся через мгновение.",
        "restarting_send_again": "🔄 Сейчас обновляюсь — подождите несколько секунд и отправьте ещё раз.",
        "update_soon_try_later": '🔧 Сейчас меня обновляют, поэтому я не могу начать ничего нового — попробуйте снова примерно через {minutes} мин. Я напишу, когда вернусь.',
        "update_soon_try_later_soon": '🔧 Сейчас меня обновляют, поэтому я не могу начать ничего нового — попробуйте снова чуть позже. Я напишу, когда вернусь.',
        "update_will_reset": '🔧 Внимание: меня скоро обновят, и то, что вы сейчас начали, будет сброшено. Через несколько минут сможете начать заново.',
        "update_done_try_now": '✅ Обновление завершено — можете пробовать снова.',
        "download_failed": "Не удалось это скачать: {error}",
        "source_blocked_server": '🚫 YouTube просит этот сервер подтвердить, что он не бот — такое он делает с серверами, а не с людьми. С вашей ссылкой всё в порядке. Попробуйте позже.',
        "source_needs_login": '🔒 Этот пост доступен только после входа в аккаунт. Instagram перестал отдавать посты серверам без авторизации, поэтому отсюда я до него не доберусь — ссылка у вас правильная, и повторная попытка ничего не изменит. Публичные посты с других сайтов по-прежнему работают.',
        "fetching": "Загрузка...",
        "reddit_fetch_failed": "Не удалось получить этот пост Reddit: {error}",
        "twitter_fetch_failed_link": "Не удалось получить содержимое поста прямо сейчас — вот ссылка: {url}",
        "unrecognized_message": (
            "Это не похоже на ссылку, которую я узнаю — Instagram, TikTok, "
            "Pinterest, Reddit или Twitter/X — пришли одну из них, чтобы скачать, "
            "или /help для списка команд."
        ),
        "unknown_command": "Я не знаю такую команду. Отправь /help, чтобы увидеть, что я умею.",
    },
}


def t(lang: str | None, key: str, **kwargs) -> str:
    table = STRINGS.get(lang) or STRINGS["en"]
    template = table.get(key) or STRINGS["en"].get(key, key)
    return template.format(**kwargs) if kwargs else template


async def get_lang(user_id: int, context) -> str:
    """Cached in context.user_data to avoid a DB round-trip on every handler
    call. Falls back to "en" for a user who hasn't chosen a language yet
    (only reachable outside /start's first-run gate, e.g. someone who sends
    a link before ever running /start)."""
    cached = context.user_data.get("lang")
    if cached:
        return cached
    lang = await asyncio.to_thread(db.get_user_language, user_id) or "en"
    context.user_data["lang"] = lang
    return lang
