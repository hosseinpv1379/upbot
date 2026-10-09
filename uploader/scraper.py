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
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp import ClientTimeout

from uploader.config import ProxyConfig
from uploader.media import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from uploader.utils import sanitize_filename

logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_CANDIDATES = 40


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
        self._capture_text = False

    def _add(self, raw_url: str, label: str = "") -> None:
        if not raw_url:
            return
        absolute = urljoin(self.base_url, raw_url.strip())
        kind = _media_kind_for(absolute)
        if not kind or absolute in self.candidates:
            return
        name = label.strip() or sanitize_filename(urlparse(absolute).path.rsplit("/", 1)[-1])
        self.candidates[absolute] = MediaCandidate(url=absolute, label=name or absolute, kind=kind)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        if tag in ("audio", "video", "source"):
            src = attrs_dict.get("src")
            if src:
                self._add(src)
        elif tag == "a":
            href = attrs_dict.get("href")
            if href:
                self._last_href = href
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

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._last_href is not None:
            label = "".join(self._pending_text)
            self._add(self._last_href, label)
            self._last_href = None
            self._capture_text = False
            self._pending_text = []


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


def parse_media_candidates(html: str, base_url: str) -> list[MediaCandidate]:
    parser = _MediaLinkParser(base_url)
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must never crash the bot
        logger.exception("Failed to parse scraped page %s", base_url)
    return list(parser.candidates.values())[:MAX_CANDIDATES]
