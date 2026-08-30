"""Download Instagram / TikTok videos with yt-dlp so the bot can re-send them.

yt-dlp is imported lazily, inside download_video(). Importing it costs
roughly 30-40 MB of resident memory and a second of startup for its
several-hundred extractor modules, and a bot that goes a whole day without
anyone pasting a link should not be paying that the whole time. The first
download pays it once, and it stays loaded from then on.
"""
import os
import re
import uuid

LINK_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?(?:instagram\.com|tiktok\.com)/\S+",
    re.IGNORECASE,
)

# Telegram itself refuses uploads over 50 MB without a local Bot API server,
# so anything larger was always going to be downloaded and then thrown away.
# Telling yt-dlp up front turns "fill the container's disk, then fail" into
# "don't start". The default leaves headroom under that 50 MB ceiling.
MAX_DOWNLOAD_MB = int(os.environ.get("DBOT_MAX_DOWNLOAD_MB", "48"))

# Nothing this bot handles is a feature film. A ceiling here is what stops a
# mis-pasted link to a three-hour stream from occupying the one worker slot
# (and the disk) for as long as it takes.
MAX_DURATION_S = int(os.environ.get("DBOT_MAX_DURATION_SECONDS", "1800"))

SOCKET_TIMEOUT_S = int(os.environ.get("DBOT_SOCKET_TIMEOUT", "30"))


class TooLarge(Exception):
    """The clip is over the size/duration ceiling. Message is user-facing."""


def find_link(text: str) -> str | None:
    match = LINK_RE.search(text)
    return match.group(0) if match else None


def _reject_long(info, *, incomplete):
    """yt-dlp's match_filter hook: runs on the *metadata*, before a single
    byte of media is fetched."""
    duration = info.get("duration")
    if duration and duration > MAX_DURATION_S:
        return (
            f"that clip is {int(duration) // 60} minutes long, and this bot "
            f"caps downloads at {MAX_DURATION_S // 60}"
        )
    return None


def download_video(url: str, out_dir: str) -> str:
    """Downloads the video at `url` into `out_dir`, returns the local file path.

    Blocking -- call it through asyncio.to_thread.
    """
    import yt_dlp  # deferred: see the module docstring

    os.makedirs(out_dir, exist_ok=True)
    unique_id = uuid.uuid4().hex
    outtmpl = os.path.join(out_dir, f"{unique_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        # Prefer a stream that already fits rather than downloading the
        # largest one available and discovering it doesn't.
        "format": (
            f"best[filesize<{MAX_DOWNLOAD_MB}M]/"
            f"best[filesize_approx<{MAX_DOWNLOAD_MB}M]/"
            "mp4/bestvideo+bestaudio/best"
        ),
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        "match_filter": _reject_long,
        "socket_timeout": SOCKET_TIMEOUT_S,
        "retries": 2,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        # Nothing here reads a cache, and the default location is a writeable
        # directory this process would otherwise keep growing.
        "cachedir": False,
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            # match_filter rejected it -- the reason is already in the log,
            # and there is no file to hand back.
            raise TooLarge(
                f"That one is too long -- this bot caps downloads at "
                f"{MAX_DURATION_S // 60} minutes and {MAX_DOWNLOAD_MB} MB."
            )
        path = ydl.prepare_filename(info)
        # merge_output_format can change the extension after download
        if not os.path.exists(path):
            base, _ = os.path.splitext(path)
            mp4_path = base + ".mp4"
            if os.path.exists(mp4_path):
                path = mp4_path
    if not os.path.exists(path):
        raise TooLarge(
            f"That file is over this bot's {MAX_DOWNLOAD_MB} MB limit, so the "
            "download was stopped."
        )
    return path
