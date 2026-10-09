"""Lightweight HTML scraper: finds direct audio/video links on a page.

No external HTML-parsing dependency — uses the stdlib ``html.parser`` to pull
``<a href>``, ``<audio>``/``<video>``/``<source src>`` and ``og:audio``/``og:video``
meta tags, then keeps only the ones whose resolved URL looks like a downloadable
media file (by extension). This intentionally does not try to sniff media type
via extra HTTP requests per link — pages with many candidate links would make
that too slow — so a site that serves media through extension-less URLs
(e.g. ``/stream/12345``) won't be picked up.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from aiohttp import ClientTimeout

from uploader.config import ProxyConfig
from uploader.media import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from uploader.utils import sanitize_filename

logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_CANDIDATES = 40
HEADING_TAGS = ("h1", "h2", "h3")

# Many Persian music-download sites reuse the same WordPress-plugin markup: the
# visible download button text is generic ("دانلود کیفیت 320" / "Download 320"),
# while the real song/artist name sits in a nearby heading (<h2>the post title</h2>)
# or a data-artist/data-song attribute on the post's wrapper element. These patterns
# let us recover a useful label instead of showing ten identical "دانلود کیفیت 320"
# buttons.
_BOILERPLATE_WORDS_RE = re.compile(
    r"(دانلود|دریافت|download|پخش|play|کیفیت|quality|آهنگ|موزیک|music|آلبوم|album|mp3|"
    r"لینک\s*مستقیم|direct\s*link)",
    re.IGNORECASE,
)
_ONLY_NUMBERS_PUNCT_RE = re.compile(r"^[\s\d\-–_/.,:kKbBpPsS]*$")
_HEADING_PREFIX_RE = re.compile(
    r"^\s*(دانلود|دریافت|download)\s*(آهنگ|موزیک|آلبوم|music|song|album)?\s*",
    re.IGNORECASE,
)
_QUALITY_RE = re.compile(r"\b(320|256|192|160|128|96|64)\s*(kbps|k)?\b", re.IGNORECASE)


def _is_generic_label(text: str) -> bool:
    if not text or not text.strip():
        return True
    stripped = _BOILERPLATE_WORDS_RE.sub("", text).strip()
    return not stripped or bool(_ONLY_NUMBERS_PUNCT_RE.match(stripped))


def _clean_heading(text: str) -> str:
    return _HEADING_PREFIX_RE.sub("", text).strip()


def _extract_quality(text: str) -> Optional[str]:
    match = _QUALITY_RE.search(text or "")
    return match.group(1) if match else None


@dataclass
class MediaCandidate:
    url: str
    label: str
    kind: str  # "audio" | "video"


def _build_connector(proxy: Optional[ProxyConfig]):
    if proxy is None:
        return None
    from aiohttp_socks import ProxyConnector

    return ProxyConnector.from_url(proxy.as_aiohttp_socks_url())


def _media_kind_for(url: str) -> Optional[str]:
    path = urlparse(url).path.lower()
    for ext in MEDIA_EXTENSIONS:
        if path.endswith(ext):
            return "audio" if ext in AUDIO_EXTENSIONS else "video"
    return None


class _MediaLinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates: dict[str, MediaCandidate] = {}
        self._pending_text: list[str] = []
        self._last_href: Optional[str] = None
        self._last_title_attr: Optional[str] = None
        self._capture_text = False

        # Context carried over from the surrounding markup, reset whenever a new
        # one is seen — each post/article block in a listing page sets its own.
        self._current_heading: Optional[str] = None
        self._current_data_label: Optional[str] = None
        self._heading_depth = 0
        self._heading_text: list[str] = []

    def _track_data_attrs(self, attrs_dict: dict) -> None:
        artist = attrs_dict.get("data-artist")
        song = attrs_dict.get("data-song") or attrs_dict.get("data-title") or attrs_dict.get("data-name")
        # Some audio-player widgets reuse "data-song" for the media URL itself —
        # only trust it as a title when it isn't a URL.
        if song and urlparse(song).scheme in ("http", "https"):
            song = None
        if song:
            self._current_data_label = f"{artist} - {song}" if artist else song

    def _resolve_label(self, raw_url: str, own_text: str, title_attr: Optional[str]) -> str:
        own_label = None
        if title_attr and not _is_generic_label(title_attr):
            own_label = title_attr.strip()
        elif own_text and not _is_generic_label(own_text):
            own_label = own_text.strip()

        if own_label:
            base = own_label
        elif self._current_data_label:
            base = self._current_data_label
        elif self._current_heading and not _is_generic_label(_clean_heading(self._current_heading)):
            base = _clean_heading(self._current_heading)
        else:
            base = sanitize_filename(unquote(urlparse(raw_url).path.rsplit("/", 1)[-1]))

        quality = _extract_quality(title_attr or own_text or "")
        if quality and quality not in base:
            base = f"{base} ({quality})"
        return base.strip() or raw_url

    def _add(self, raw_url: str, own_text: str = "", title_attr: Optional[str] = None) -> None:
        if not raw_url:
            return
        absolute = urljoin(self.base_url, raw_url.strip())
        kind = _media_kind_for(absolute)
        if not kind or absolute in self.candidates:
            return
        label = self._resolve_label(absolute, own_text, title_attr)
        self.candidates[absolute] = MediaCandidate(url=absolute, label=label, kind=kind)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)

        if tag == "article":
            # <article> is the standard boundary for "one post/listing entry" in
            # essentially every blog/loop-based theme — reset here (before reading
            # this article's own data-* attrs below) so a data-song picked up for
            # the previous entry can't leak into one that doesn't set it itself.
            self._current_data_label = None

        if tag in HEADING_TAGS:
            self._heading_depth += 1
            self._heading_text = []

        self._track_data_attrs(attrs_dict)
        if tag in ("audio", "video", "source"):
            src = attrs_dict.get("src")
            if src:
                self._add(src, title_attr=attrs_dict.get("title"))
        elif tag == "a":
            href = attrs_dict.get("href")
            if href:
                self._last_href = href
                self._last_title_attr = attrs_dict.get("title")
                self._capture_text = True
                self._pending_text = []
        elif tag == "meta":
            prop = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            if prop in ("og:audio", "og:video", "og:audio:url", "og:video:url", "og:audio:secure_url"):
                content = attrs_dict.get("content")
                if content:
                    self._add(content)

    def handle_data(self, data: str) -> None:
        if self._capture_text:
            self._pending_text.append(data)
        if self._heading_depth > 0:
            self._heading_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._last_href is not None:
            label = "".join(self._pending_text)
            self._add(self._last_href, label, self._last_title_attr)
            self._last_href = None
            self._last_title_attr = None
            self._capture_text = False
            self._pending_text = []
        elif tag in HEADING_TAGS:
            self._heading_depth = max(0, self._heading_depth - 1)
            if self._heading_depth == 0:
                text = "".join(self._heading_text).strip()
                if text:
                    self._current_heading = text
                self._heading_text = []


async def fetch_if_html(
    url: str, proxy: Optional[ProxyConfig], user_agent: str
) -> tuple[Optional[str], str]:
    """Fetches the URL; returns (html_text, final_url) if it served HTML, else (None, url)."""
    connector = _build_connector(proxy)
    timeout = ClientTimeout(total=20)
    headers = {"User-Agent": user_agent}
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                content_type = resp.headers.get("Content-Type", "")
                final_url = str(resp.url)
                if "html" not in content_type.lower():
                    resp.release()
                    return None, final_url

                chunks: list[str] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    chunks.append(chunk.decode("utf-8", errors="ignore"))
                    if total >= MAX_PAGE_BYTES:
                        break
                return "".join(chunks), final_url
    except Exception:  # noqa: BLE001 - a broken page must never break the message handler
        logger.exception("Failed to fetch %s for scraping", url)
        return None, url


def _disambiguate_labels(candidates: list[MediaCandidate]) -> list[MediaCandidate]:
    """Safety net: if the heuristics above still produced identical labels for
    distinct files (an unrecognized site layout), number them so every button is
    at least distinguishable instead of silently showing N identical buttons."""
    seen: dict[str, int] = {}
    for c in candidates:
        seen[c.label] = seen.get(c.label, 0) + 1
    if all(count == 1 for count in seen.values()):
        return candidates

    counters: dict[str, int] = {}
    result = []
    for c in candidates:
        if seen[c.label] > 1:
            counters[c.label] = counters.get(c.label, 0) + 1
            c = MediaCandidate(url=c.url, label=f"{c.label} ({counters[c.label]})", kind=c.kind)
        result.append(c)
    return result


def parse_media_candidates(html: str, base_url: str) -> list[MediaCandidate]:
    parser = _MediaLinkParser(base_url)
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must never crash the bot
        logger.exception("Failed to parse scraped page %s", base_url)
    candidates = list(parser.candidates.values())[:MAX_CANDIDATES]
    return _disambiguate_labels(candidates)
