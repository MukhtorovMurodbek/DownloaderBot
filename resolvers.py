"""Turning a pasted link into files, through whichever route is still working.

WHY THIS EXISTS
    Until v1.2.1 this bot had exactly one way to get a video: yt-dlp, running
    inside the container, fetching from the site directly. That works from a
    phone and does not work from Railway, and the difference is not the code
    -- it is the IP address. Instagram, YouTube and increasingly TikTok decide
    what to serve partly from *where* the request comes from, every cloud
    host's address sits in a published datacenter range, and those ranges get
    a login page where a home connection gets the post. v1.1.3 diagnosed this
    correctly and concluded there were two fixes, cookies or a residential
    proxy, both of which need something the bot cannot supply itself. So
    Instagram simply stopped working, and said so politely.

    There is a third option, and it is the one that holds: do not make the
    request from here at all. The "fix the embed" services and the public
    downloader APIs run their own fetching infrastructure -- their addresses,
    their cookie pools, their problem -- and hand back either a direct CDN URL
    or a copy proxied through themselves. Whether *this* container is on a
    blocked range stops mattering.

    The catch is that any one of those services is mortal. They get bought,
    rate-limited, sued (vxtiktok, as of this writing, answers every request
    with "due to a legal request, this service is no longer available"), or
    just stop resolving in DNS (ddinstagram and kkinstagram both did). A bot
    built on one of them is a bot that breaks in a month with no warning.

    Hence a chain, per platform, with the bot keeping score. Every provider is
    tried in turn until one produces media; whichever one worked is
    remembered, whichever one failed is remembered too, and a provider that
    keeps failing is moved to the back of the queue and retried more and more
    rarely instead of being taken out of service. Nothing here needs a human
    to notice a provider has died. Adding a replacement is a function and one
    line in PROVIDERS.

WHAT IS NOT SOLVED HERE
    Nothing makes a private account public, and nothing makes a deleted post
    come back. When every provider says no, the user gets one sentence saying
    which of those two kinds of "no" it looked like, not five stack traces.

    And the providers were verified from a residential connection, because
    that is where this was written. A service that answers here could still be
    challenged from Railway's range -- which is exactly the failure the health
    scoring is built to absorb: the first user to hit it pays a few seconds,
    the chain moves on, and `/providers` says what happened.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

import net
from net import CRAWLER_UA, FetchError

logger = logging.getLogger(__name__)

# How long any single provider gets before the chain gives up on it and tries
# the next. Deliberately short: the whole point of a chain is that waiting out
# a dead provider costs more than moving on, and four providers at 15 s each
# is still inside what a user will sit through.
PROVIDER_TIMEOUT_S = float(os.environ.get("DBOT_PROVIDER_TIMEOUT", "20"))

# After this many consecutive failures a provider starts being skipped -- not
# dropped. See _ordered(): it goes to the back of the queue and is still tried
# if everything ahead of it fails, which is what lets a service that comes
# back from the dead be noticed without anybody redeploying.
COOLDOWN_AFTER_FAILURES = int(os.environ.get("DBOT_PROVIDER_COOLDOWN_AFTER", "3"))
COOLDOWN_BASE_S = float(os.environ.get("DBOT_PROVIDER_COOLDOWN_BASE", "120"))
COOLDOWN_MAX_S = float(os.environ.get("DBOT_PROVIDER_COOLDOWN_MAX", "3600"))


# ---------------------------------------------------------------------------
# What a resolver hands back
# ---------------------------------------------------------------------------

@dataclass
class MediaItem:
    """One file. Either a URL to fetch (`url`) or something already on disk
    (`path`) -- yt-dlp produces the second kind because it does its own
    downloading and there is no sense making it hand back a URL it has
    already spent the request on."""
    kind: str                      # "video" | "photo" | "audio"
    url: str | None = None
    # Other URLs for the *same* file, tried in order if `url` will not serve.
    # tikwm hands back both TikTok's own CDN and a copy proxied through
    # tikwm: the first is faster when it works and the second is the entire
    # reason the provider is useful when it does not.
    alt_urls: list[str] = field(default_factory=list)
    path: str | None = None
    filename: str = "download"
    headers: dict = field(default_factory=dict)


@dataclass
class Resolved:
    platform: str
    provider: str
    items: list[MediaItem]
    title: str | None = None
    author: str | None = None

    @property
    def has_video(self) -> bool:
        return any(i.kind == "video" for i in self.items)


class ProviderFailed(Exception):
    """This provider could not do it. Try the next one.

    `kind` is a hint for the sentence the user eventually sees if *every*
    provider fails: "blocked" (the source turned the server away), "missing"
    (the post looks gone or private), "too_big", or "error".
    """

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


class NothingWorked(Exception):
    """Every provider for this platform failed. Carries the per-provider
    reasons for the log and the best guess at which sentence to show."""

    def __init__(self, platform: str, failures: dict[str, ProviderFailed]):
        self.platform = platform
        self.failures = failures
        super().__init__(
            f"{platform}: " + "; ".join(f"{n}: {e}" for n, e in failures.items())
        )

    @property
    def kind(self) -> str:
        """The most specific verdict any provider reached.

        "missing" wins over "blocked" wins over the rest, because a provider
        that got far enough to be told "this post does not exist" learned
        something the ones that were bounced at the door did not.
        """
        kinds = {e.kind for e in self.failures.values()}
        for preferred in ("missing", "too_big", "blocked"):
            if preferred in kinds:
                return preferred
        return "error"


# ---------------------------------------------------------------------------
# Keeping score
# ---------------------------------------------------------------------------

@dataclass
class Health:
    ok: int = 0
    failed: int = 0
    streak: int = 0                 # consecutive failures
    last_ok: float | None = None    # unix seconds
    last_fail: float | None = None
    last_error: str | None = None

    @property
    def cooling_until(self) -> float:
        """When this provider becomes a first-class citizen again.

        Exponential from the third consecutive failure, capped at an hour, so
        a service that went away this morning is asked about roughly hourly
        rather than on every single request -- and a service having a bad
        minute is back in the rotation two minutes later.
        """
        if self.streak < COOLDOWN_AFTER_FAILURES or self.last_fail is None:
            return 0.0
        over = self.streak - COOLDOWN_AFTER_FAILURES
        return self.last_fail + min(COOLDOWN_BASE_S * (2 ** over), COOLDOWN_MAX_S)


_health: dict[str, Health] = {}
_health_dirty: set[str] = set()


def health(name: str) -> Health:
    return _health.setdefault(name, Health())


def _record_ok(name: str) -> None:
    h = health(name)
    h.ok += 1
    h.streak = 0
    h.last_ok = time.time()
    h.last_error = None
    _health_dirty.add(name)


def _record_fail(name: str, error: str) -> None:
    h = health(name)
    h.failed += 1
    h.streak += 1
    h.last_fail = time.time()
    h.last_error = error[:300]
    _health_dirty.add(name)


def snapshot() -> dict[str, Health]:
    """Everything known, for /providers and for the periodic flush."""
    return dict(_health)


def take_dirty() -> dict[str, Health]:
    """The rows that changed since the last flush, and clears the flag.

    Health is kept in memory and written to Postgres on a timer rather than on
    every attempt, for the same reason activity events are batched: this bot's
    database is a shared one and a write per download is a write per download
    forever. Losing the last minute of counters in a crash costs nothing --
    they are advice about which provider to try first, not user data.
    """
    dirty = {name: _health[name] for name in _health_dirty if name in _health}
    _health_dirty.clear()
    return dirty


def load(rows) -> None:
    """Seed from the database at startup, so a redeploy does not go back to
    hammering a provider that has been dead for a week."""
    for row in rows:
        name = row["provider"]
        _health[name] = Health(
            ok=row.get("ok_count") or 0,
            failed=row.get("fail_count") or 0,
            streak=row.get("consecutive_fails") or 0,
            last_ok=row.get("last_ok_at"),
            last_fail=row.get("last_fail_at"),
            last_error=row.get("last_error"),
        )


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

_OG_TAG_RE = re.compile(r"<meta[^>]+>", re.IGNORECASE)


def _meta(html: str, prop: str) -> str | None:
    """Read one OpenGraph/Twitter-card meta tag's content.

    Attribute order is not guaranteed -- Instagram's own markup puts
    `content=` before `property=` -- so this isolates the whole tag first and
    then pulls `content` out of it, rather than assuming an order.
    """
    for tag in _OG_TAG_RE.findall(html):
        if re.search(rf'(?:property|name)=["\']{re.escape(prop)}["\']', tag, re.I):
            m = re.search(r'content=["\']([^"\']+)["\']', tag, re.I)
            if m:
                return m.group(1).replace("&amp;", "&")
    return None


async def _get(url: str, *, crawler: bool = False, **kwargs) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    if crawler:
        headers.setdefault("User-Agent", CRAWLER_UA)
    return await net.client().get(url, headers=headers, **kwargs)


def _ext_of(name: str, default: str) -> str:
    _, _, ext = name.rpartition(".")
    return ext.lower() if ext and len(ext) <= 5 else default


def _kind_of(name: str) -> str:
    ext = _ext_of(name, "")
    if ext in ("mp4", "mov", "webm", "mkv", "m4v"):
        return "video"
    if ext in ("mp3", "m4a", "ogg", "opus", "wav"):
        return "audio"
    return "photo"


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

INSTAGRAM_CODE_RE = re.compile(
    r"instagram\.com/(?:[^/]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE
)


def instagram_code(url: str) -> str | None:
    m = INSTAGRAM_CODE_RE.search(url)
    return m.group(1) if m else None


def _jwt_payload(token: str) -> dict:
    """downloadgram wraps every media URL in an unsigned-to-us JWT whose
    payload carries the filename and the real source URL. Read, never
    verified -- it is not this bot's token and the signature is between them
    and their own CDN. Only the filename is used, to tell a video from a
    cover image."""
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


async def _ig_downloadgram(url: str) -> Resolved:
    """downloadgram.org's public endpoint.

    The most useful property is not that it works -- it is that the media
    comes back proxied through `cdn.downloadgram.org`, so the *second* request
    (actually fetching the file) does not touch Instagram's CDN from this
    container either. Both halves of the problem are somebody else's.

    The response is an obfuscated JavaScript blob that assigns innerHTML; the
    URLs are in there with their forward slashes hex-escaped. Parsed by
    pulling the token links out rather than by pretending to understand the
    JavaScript.
    """
    resp = await _get("https://api.downloadgram.org/media", params={"url": url})
    if resp.status_code == 400:
        raise ProviderFailed("downloadgram rejected the link", "missing")
    resp.raise_for_status()
    body = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), resp.text)

    tokens: list[str] = []
    for tok in re.findall(r"https://cdn\.downloadgram\.org/\?token=([A-Za-z0-9_.-]+)", body):
        if tok not in tokens:
            tokens.append(tok)
    if not tokens:
        raise ProviderFailed("downloadgram returned no media", "missing")

    items: list[MediaItem] = []
    for tok in tokens:
        try:
            name = str(_jwt_payload(tok).get("filename") or "")
        except Exception:
            name = ""
        items.append(MediaItem(
            kind=_kind_of(name),
            url="https://cdn.downloadgram.org/?token=" + tok,
            filename=name or f"instagram.{_ext_of(name, 'mp4')}",
        ))

    # A reel comes back as [cover.jpg, video.mp4]. Sending both would mean
    # handing someone a still of the video they just asked for, next to the
    # video. If there is any video in the set, the photos are thumbnails.
    if any(i.kind == "video" for i in items):
        items = [i for i in items if i.kind == "video"]
    return Resolved("instagram", "downloadgram", items)


async def _ig_instafix(url: str) -> Resolved:
    """An InstaFix instance (`eeinstagram.com`).

    InstaFix is the open-source thing behind the ddinstagram/kkinstagram
    links people paste to make Instagram embeds work in Discord and Telegram;
    it answers a crawler User-Agent with OpenGraph tags pointing at the media.
    Both of its better-known domains stopped resolving, which is the whole
    argument for this file.

    Its `/videos/<code>/N` endpoint sometimes redirects to the post's *cover
    frame* rather than the video -- a JPEG where an MP4 was promised. So what
    it says is checked against what it serves before the item is accepted,
    rather than sending the user a still and calling it a download.
    """
    code = instagram_code(url)
    if not code:
        raise ProviderFailed("not an Instagram post URL", "missing")
    host = os.environ.get("DBOT_INSTAFIX_HOST", "https://eeinstagram.com")
    resp = await _get(f"{host}/p/{code}/", crawler=True)
    resp.raise_for_status()
    html = resp.text

    video = _meta(html, "og:video") or _meta(html, "twitter:player:stream")
    image = _meta(html, "og:image")
    target = video or image
    if not target:
        raise ProviderFailed("InstaFix had no media for that post", "missing")
    target = urljoin(host, target)

    # Ask for one byte. Enough to read the Content-Type without pulling the
    # file twice, and cheaper than trusting the meta tag.
    probe = await net.client().get(target, headers={"Range": "bytes=0-0"})
    ctype = probe.headers.get("content-type", "")
    if video and not ctype.startswith("video/"):
        raise ProviderFailed(
            f"InstaFix promised a video and served {ctype or 'nothing'}", "blocked"
        )
    kind = "video" if ctype.startswith("video/") else "photo"
    ext = "mp4" if kind == "video" else "jpg"
    return Resolved("instagram", "instafix",
                    [MediaItem(kind, url=target, filename=f"instagram_{code}.{ext}")])


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------

async def _tt_tikwm(url: str) -> Resolved:
    """tikwm.com's open API. Resolves `vm.`/`vt.` short links itself, strips
    the watermark, and -- the part that matters here -- also exposes the same
    media through its own host at `/video/media/<play|hdplay>/<id>.mp4`, which
    is used as the fallback when TikTok's CDN will not serve this container
    directly. Photo slideshows come back in `images`."""
    resp = await _get("https://tikwm.com/api/", params={"url": url, "hd": 1})
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise ProviderFailed(payload.get("msg") or "tikwm said no", "missing")
    data = payload.get("data") or {}
    vid = str(data.get("id") or "")

    images = data.get("images") or []
    if images:
        items = [MediaItem("photo", url=u, filename=f"tiktok_{vid}_{n}.jpg")
                 for n, u in enumerate(images[:20], start=1)]
    else:
        play = data.get("hdplay") or data.get("play") or data.get("wmplay")
        if not play:
            raise ProviderFailed("tikwm returned no playable media", "missing")
        which = "hdplay" if data.get("hdplay") else "play"
        alts = [f"https://tikwm.com/video/media/{which}/{vid}.mp4"] if vid else []
        items = [MediaItem("video", url=play, alt_urls=alts,
                           filename=f"tiktok_{vid or 'video'}.mp4")]
    author = (data.get("author") or {}).get("unique_id")
    return Resolved("tiktok", "tikwm", items, title=data.get("title"),
                    author=f"@{author}" if author else None)


async def _tt_tnktok(url: str) -> Resolved:
    """tnktok.com, the TikTok embed-fixer. Same crawler-UA trick as InstaFix,
    and it serves the video off its own `offload.` host rather than TikTok's,
    which is the useful half."""
    resp = await _get(re.sub(r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com",
                             "https://tnktok.com", url, flags=re.I), crawler=True)
    resp.raise_for_status()
    video = _meta(resp.text, "og:video") or _meta(resp.text, "twitter:player:stream")
    if not video:
        image = _meta(resp.text, "og:image")
        if not image:
            raise ProviderFailed("tnktok had no media for that link", "missing")
        return Resolved("tiktok", "tnktok", [MediaItem("photo", url=image,
                                                       filename="tiktok.jpg")])
    return Resolved("tiktok", "tnktok",
                    [MediaItem("video", url=video, filename="tiktok.mp4")])


# ---------------------------------------------------------------------------
# Twitter / X
# ---------------------------------------------------------------------------

TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")


def tweet_id(url: str) -> str | None:
    m = TWEET_ID_RE.search(url)
    return m.group(1) if m else None


async def _tw_api(url: str, host: str, provider: str) -> Resolved:
    """fxtwitter and vxtwitter share a response shape close enough to read
    with one function. Both are the community embed-fixers for X, both expose
    a JSON API their own front ends use, and neither needs a key."""
    tid = tweet_id(url)
    if not tid:
        raise ProviderFailed("not a tweet URL", "missing")
    resp = await _get(f"{host}/i/status/{tid}")
    if resp.status_code == 403:
        raise ProviderFailed(f"{provider} is behind a challenge", "blocked")
    if resp.status_code == 404:
        raise ProviderFailed("that post is gone or protected", "missing")
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderFailed(f"{provider} did not return JSON", "blocked") from exc

    tweet = data.get("tweet") if isinstance(data.get("tweet"), dict) else data
    items: list[MediaItem] = []
    media = (tweet.get("media") or {}).get("all") or tweet.get("media_extended") or []
    for n, m in enumerate(media, start=1):
        mtype = (m.get("type") or "").lower()
        murl = m.get("url")
        if not murl:
            continue
        if mtype in ("video", "gif", "animated_gif"):
            items.append(MediaItem("video", url=murl, filename=f"tweet_{tid}_{n}.mp4"))
        else:
            items.append(MediaItem("photo", url=murl, filename=f"tweet_{tid}_{n}.jpg"))
    if not items:
        for murl in tweet.get("mediaURLs") or []:
            items.append(MediaItem(_kind_of(urlparse(murl).path), url=murl,
                                   filename=f"tweet_{tid}.{_ext_of(urlparse(murl).path, 'jpg')}"))
    if not items:
        raise ProviderFailed("that post has no media", "missing")

    author = tweet.get("author") or {}
    handle = author.get("screen_name") or tweet.get("user_screen_name")
    return Resolved("twitter", provider, items, title=tweet.get("text"),
                    author=f"@{handle}" if handle else None)


async def _tw_fxtwitter(url: str) -> Resolved:
    return await _tw_api(url, "https://api.fxtwitter.com", "fxtwitter")


async def _tw_vxtwitter(url: str) -> Resolved:
    return await _tw_api(url, "https://api.vxtwitter.com", "vxtwitter")


# ---------------------------------------------------------------------------
# Pinterest
# ---------------------------------------------------------------------------

async def _pin_og(url: str) -> Resolved:
    """A pin's own page still carries og:image (and og:video for video pins)
    without a login. Pinterest is the one platform here that never needed a
    workaround, which is worth remembering the next time one of the others
    looks unfixable."""
    resp = await _get(url)
    resp.raise_for_status()
    video = _meta(resp.text, "og:video") or _meta(resp.text, "og:video:secure_url")
    if video:
        return Resolved("pinterest", "og", [MediaItem("video", url=video,
                                                      filename="pinterest.mp4")])
    image = _meta(resp.text, "og:image")
    if not image:
        raise ProviderFailed("that pin has no image or video tag", "missing")
    # og:image points at a resized copy -- `.../736x/ab/cd/....jpg`. The same
    # file is on the same host under `originals`, usually several times the
    # resolution, and downloading someone a thumbnail when the full picture is
    # one path segment away is the sort of thing nobody notices until they
    # try to print it. Not every pin has an `originals` copy, hence the
    # resized one as the fallback rather than the only option.
    original = re.sub(r"/(?:\d{2,4}x\d{0,4}|originals)/", "/originals/", image, count=1)
    routes = [original, image] if original != image else [image]
    return Resolved("pinterest", "og",
                    [MediaItem("photo", url=routes[0], alt_urls=routes[1:],
                               filename=f"pinterest.{_ext_of(urlparse(image).path, 'jpg')}")])


# ---------------------------------------------------------------------------
# yt-dlp, the universal last resort
# ---------------------------------------------------------------------------

def _ytdlp_provider(platform: str):
    async def provider(url: str) -> Resolved:
        import video  # deferred with yt-dlp itself; see video.py's docstring
        try:
            path = await asyncio.to_thread(video.download_video, url, _WORK_DIR)
        except video.BlockedBySource as exc:
            raise ProviderFailed(str(exc), "blocked") from exc
        except video.TooLarge as exc:
            raise ProviderFailed(str(exc), "too_big") from exc
        except Exception as exc:
            raise ProviderFailed(str(exc), "error") from exc
        return Resolved(platform, "ytdlp",
                        [MediaItem("video", path=path,
                                   filename=os.path.basename(path))])
    return provider


_WORK_DIR = os.environ.get("DBOT_WORK_DIR") or tempfile.gettempdir()


# ---------------------------------------------------------------------------
# The chains
# ---------------------------------------------------------------------------
# Order is the order they are tried on a healthy day: verified-working first,
# then the ones that are known to be more fragile, then yt-dlp -- which is
# last everywhere rather than first because it is the one route that makes the
# request from this container's own address, and that address is the problem.
# It is still in every chain, because it is also the only one that gets better
# the moment DBOT_*_COOKIES_FILE or DBOT_*_PROXY is set.

PROVIDERS: dict[str, list[tuple[str, object]]] = {
    "instagram": [
        ("downloadgram", _ig_downloadgram),
        ("instafix", _ig_instafix),
        ("ytdlp:instagram", _ytdlp_provider("instagram")),
    ],
    "tiktok": [
        ("tikwm", _tt_tikwm),
        ("tnktok", _tt_tnktok),
        ("ytdlp:tiktok", _ytdlp_provider("tiktok")),
    ],
    "twitter": [
        ("vxtwitter", _tw_vxtwitter),
        ("fxtwitter", _tw_fxtwitter),
        ("ytdlp:twitter", _ytdlp_provider("twitter")),
    ],
    "pinterest": [
        ("pinterest_og", _pin_og),
        ("ytdlp:pinterest", _ytdlp_provider("pinterest")),
    ],
    # Reddit has one entry on purpose. bot.py reads the post itself -- it
    # needs the title, author and score for the card anyway -- and sends a
    # plain image post straight from `url_overridden_by_dest`. What is left
    # for the chain is hosted video, which is DASH with a separate audio
    # track, and yt-dlp is the only thing here that muxes it.
    "reddit": [
        ("ytdlp:reddit", _ytdlp_provider("reddit")),
    ],
    "youtube": [
        ("ytdlp:youtube", _ytdlp_provider("youtube")),
    ],
}


def _ordered(platform: str) -> list[tuple[str, object]]:
    """Registry order, with the currently-unwell moved to the back.

    Not filtered -- moved. A provider in cooldown is still tried if
    everything ahead of it failed, so a chain whose every member is having a
    bad day still does the most useful thing available instead of refusing on
    the strength of yesterday's scores.
    """
    now = time.time()
    ready, cooling = [], []
    for entry in PROVIDERS.get(platform, []):
        h = health(entry[0])
        (cooling if h.cooling_until > now else ready).append(entry)
    return ready + cooling


async def resolve(platform: str, url: str) -> Resolved:
    """Walk the chain until something produces media."""
    failures: dict[str, ProviderFailed] = {}
    chain = _ordered(platform)
    if not chain:
        raise NothingWorked(platform, {})

    for name, fn in chain:
        started = time.perf_counter()
        try:
            resolved = await asyncio.wait_for(fn(url), timeout=PROVIDER_TIMEOUT_S)
            if not resolved.items:
                raise ProviderFailed("returned nothing", "missing")
        except ProviderFailed as exc:
            failures[name] = exc
            _record_fail(name, str(exc))
            logger.info("provider %s failed for %s: %s", name, platform, exc)
        except asyncio.TimeoutError:
            exc = ProviderFailed(f"timed out after {PROVIDER_TIMEOUT_S:.0f}s", "error")
            failures[name] = exc
            _record_fail(name, str(exc))
            logger.info("provider %s timed out for %s", name, platform)
        except Exception as exc:  # a provider's own bug must not end the chain
            wrapped = ProviderFailed(f"{type(exc).__name__}: {exc}", "error")
            failures[name] = wrapped
            _record_fail(name, str(wrapped))
            logger.warning("provider %s raised for %s", name, platform, exc_info=True)
        else:
            _record_ok(name)
            logger.info("provider %s resolved %s in %.1fs (%d item(s))",
                        name, platform, time.perf_counter() - started,
                        len(resolved.items))
            return resolved

    raise NothingWorked(platform, failures)


# ---------------------------------------------------------------------------
# Getting the bytes
# ---------------------------------------------------------------------------

async def download(item: MediaItem, work_dir: str | None = None) -> str:
    """Put one MediaItem on disk and return the path.

    Already-local items (yt-dlp's) are handed straight back. Everything else
    is streamed, with a `Referer` some CDNs want and a size ceiling that stops
    a mis-detected link from filling the container's disk. `alt_urls` are
    other routes to the same file and are tried in order.
    """
    if item.path:
        return item.path
    routes = [u for u in [item.url, *item.alt_urls] if u]
    if not routes:
        raise FetchError("Nothing to download.")

    work_dir = work_dir or _WORK_DIR
    os.makedirs(work_dir, exist_ok=True)
    ext = _ext_of(item.filename, "mp4" if item.kind == "video" else "jpg")

    last: Exception | None = None
    for route in routes:
        path = os.path.join(work_dir, f"{uuid.uuid4().hex}.{ext}")
        headers = dict(item.headers)
        parsed = urlparse(route)
        headers.setdefault("Referer", f"{parsed.scheme}://{parsed.netloc}/")
        try:
            await net.stream_to_file(route, path, headers=headers)
        except FetchError:
            # Over the size ceiling. Another route to the same file will be
            # the same size, so there is nothing to gain by trying it.
            if os.path.exists(path):
                os.remove(path)
            raise
        except Exception as exc:
            if os.path.exists(path):
                os.remove(path)
            last = exc
            logger.info("media route failed (%s: %s), trying the next one",
                        type(exc).__name__, exc)
            continue
        item.path = path
        return path
    raise FetchError(str(last) if last else "Nothing to download.")
