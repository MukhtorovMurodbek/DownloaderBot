"""Link recognition + per-platform fetch logic for everything beyond the
original Instagram/TikTok video path (video.py, unchanged). Each platform
gets its own small async function; bot.py decides how to present the
result (video file, direct image, or a rendered card via cards.py).

Design note on auth: Instagram *photo* posts and Pinterest *board*
scraping both require a logged-in session's cookies to work reliably
(Instagram has required auth for essentially all scraping since mid-2023;
Pinterest's own API is similar for bulk access) -- deliberately left out
here. Everything below either needs no auth at all (Pinterest single-pin
images via og:image, Reddit's public .json endpoint, X's public
syndication endpoint used by its own embed widgets) or reuses the existing
yt-dlp video path, which needs no auth for public posts either.
"""
import math
import re

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ---------------------------------------------------------------------------
# One HTTP client for the whole process
# ---------------------------------------------------------------------------
# Every fetch below used to build its own httpx.AsyncClient inside an `async
# with`, which means a fresh TCP connection and TLS handshake per request and
# throwing the connection away straight after -- the same mistake the
# database layer was making, against hosts that are considerably further
# away. One client, kept open, reuses connections across requests and across
# users; the pool is small on purpose, because this bot is never fetching
# from more than a handful of hosts at once.
MAX_RESPONSE_BYTES = 25 * 1024 * 1024


class FetchError(Exception):
    """Raised with a message that's safe to show the user as-is."""


_client: "httpx.AsyncClient | None" = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": UA},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4,
                                keepalive_expiry=30.0),
        )
    return _client


async def close_client() -> None:
    """Called from the bot's shutdown hook."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def fetch_bytes(url: str, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """GET a URL, refusing anything that would not fit in a Telegram upload
    anyway. Streamed and counted as it arrives, so a server that lies about
    (or omits) Content-Length still cannot make this process hold an
    unbounded amount of data in memory."""
    async with client().stream("GET", url) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and int(declared) > limit:
            raise FetchError("That file is too big to send through Telegram.")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise FetchError("That file is too big to send through Telegram.")
            chunks.append(chunk)
    return b"".join(chunks)

INSTAGRAM_TIKTOK_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?(?:instagram\.com|tiktok\.com)/\S+", re.IGNORECASE
)
PINTEREST_RE = re.compile(r"https?://(?:www\.)?(?:pinterest\.[a-z.]+|pin\.it)/\S+", re.IGNORECASE)
REDDIT_RE = re.compile(r"https?://(?:www\.|old\.|m\.)?reddit\.com/r/\S+|https?://redd\.it/\S+", re.IGNORECASE)
TWITTER_RE = re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+", re.IGNORECASE)
# Deliberately undocumented everywhere else (see downloader_bot/bot.py's
# HELP_TEXT and BOT_COMMANDS) -- handled if someone pastes one, never
# mentioned in any command list or help text.
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)\S+", re.IGNORECASE
)

# Ordered so the most specific/common patterns are tried first.
_PLATFORM_PATTERNS = [
    ("instagram_tiktok", INSTAGRAM_TIKTOK_RE),
    ("youtube", YOUTUBE_RE),
    ("reddit", REDDIT_RE),
    ("twitter", TWITTER_RE),
    ("pinterest", PINTEREST_RE),
]

ANY_LINK_RE = re.compile(
    "|".join(f"(?:{p.pattern})" for _, p in _PLATFORM_PATTERNS), re.IGNORECASE
)


def detect_platform(text: str) -> tuple[str, str] | None:
    """Returns (platform, matched_url) for the first recognized link in
    text, or None if nothing matches."""
    for name, pattern in _PLATFORM_PATTERNS:
        m = pattern.search(text)
        if m:
            return name, m.group(0)
    return None


# ---------- Pinterest: single-pin image, no auth ----------

async def fetch_pinterest_image(url: str) -> bytes | None:
    """Pulls a pin's full-res image straight from its public og:image meta
    tag. Returns None (not bytes) if the pin doesn't have one -- most
    likely a video pin, which the caller should fall back to yt-dlp for."""
    resp = await client().get(url, timeout=15)
    resp.raise_for_status()
    html = resp.text

    # Attribute order in the actual tag isn't guaranteed (Pinterest's own
    # markup puts content= *before* property=) -- isolate the whole <meta>
    # tag containing property="og:image" first, then pull content= out of
    # that, instead of assuming a fixed order.
    tag_m = re.search(r'<meta[^>]*property="og:image"[^>]*>', html)
    if not tag_m:
        return None
    content_m = re.search(r'content="([^"]+)"', tag_m.group(0))
    if not content_m:
        return None
    img_url = content_m.group(1).replace("&amp;", "&")

    return await fetch_bytes(img_url)


# ---------- Reddit: public .json endpoint, no auth ----------

async def fetch_reddit_post(url: str) -> dict:
    """Reddit's own public .json endpoint on any post URL -- what old.reddit
    and countless tools already rely on, no login needed for public
    subreddits. Reddit's anti-scraping has gotten more aggressive about
    this over time though (IP-reputation based, not just User-Agent), so
    this can 403 from some hosts even for a perfectly public post -- that's
    surfaced as a FetchError with a clear message, not a stack trace."""
    json_url = url.split("?")[0].rstrip("/") + ".json"
    headers = {
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = await client().get(json_url, headers=headers, timeout=15)
    if resp.status_code == 403:
        raise FetchError(
            "Reddit blocked this request (their anti-scraping, not a bug here) -- "
            "try again later, or open the link directly."
        )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data[0]["data"]["children"][0]["data"]
    except (IndexError, KeyError, TypeError) as exc:
        raise FetchError("Couldn't read that Reddit post -- it may have been removed.") from exc


def reddit_is_media(post: dict) -> bool:
    hint = post.get("post_hint", "")
    return bool(post.get("is_video")) or hint in ("image", "hosted:video", "rich:video")


def reddit_direct_image_url(post: dict) -> str | None:
    """A plain-image post's direct URL, if this isn't a gallery/video."""
    if post.get("post_hint") == "image":
        return post.get("url_overridden_by_dest") or post.get("url")
    return None


# ---------- Twitter/X: public syndication endpoint, no auth ----------
# This is the same unauthenticated endpoint X's own embed widget
# (platform.twitter.com/widgets.js) calls to render an embedded tweet --
# not a private/reverse-engineered API. Best-effort by nature: X can
# rate-limit or change this without notice, which is exactly why the
# caller falls back to "here's the link" rather than erroring outright.

def _tweet_id_from_url(url: str) -> str | None:
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


def _base36(x: float, precision: int = 60) -> str:
    """Python doesn't have JS's Number.prototype.toString(36) built in --
    this reimplements it (integer part + fractional digits) just precisely
    enough to reproduce the syndication token below."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    int_part = int(x)
    frac_part = x - int_part
    int_str = "0"
    if int_part > 0:
        int_str = ""
        n = int_part
        while n > 0:
            int_str = digits[n % 36] + int_str
            n //= 36
    if frac_part <= 0:
        return int_str
    frac_str = ""
    f = frac_part
    for _ in range(precision):
        f *= 36
        d = int(f)
        frac_str += digits[d]
        f -= d
        if f <= 0:
            break
    return f"{int_str}.{frac_str}"


def _syndication_token(tweet_id: str) -> str:
    """X's embed widget (platform.twitter.com/widgets.js) computes this
    the same way to authorize an otherwise-open syndication request --
    not a secret, just an obfuscation step: ((id / 1e15) * pi) in base 36,
    with zeros and the decimal point stripped."""
    n = (int(tweet_id) / 1e15) * math.pi
    return re.sub(r"(0+|\.)", "", _base36(n))


async def fetch_tweet_syndication(url: str) -> dict | None:
    tweet_id = _tweet_id_from_url(url)
    if not tweet_id:
        return None
    token = _syndication_token(tweet_id)
    api_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token={token}&lang=en"
    try:
        resp = await client().get(api_url, timeout=15)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    # Deleted/protected/age-restricted tweets come back as a "tombstone"
    # with a moderation notice instead of real content -- treat that the
    # same as "couldn't fetch it" rather than rendering the notice as if
    # it were the tweet.
    if not data or data.get("__typename") != "Tweet":
        return None
    return data
