"""Streaming, resumable, optionally segmented file downloader built on aiohttp."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp
from aiohttp import ClientTimeout

from uploader.config import ProxyConfig
from uploader.utils import free_disk_space, resolve_filename

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024
MIN_SEGMENT_SIZE = 8 * 1024 * 1024
MAX_RETRIES = 5
BACKOFF_BASE = 1.5


class DownloadCancelled(Exception):
    """Raised internally when a user cancels an in-progress download."""


class InsufficientDiskSpace(Exception):
    pass


@dataclass
class DownloadInfo:
    size: int
    accept_ranges: bool
    filename: str
    content_type: Optional[str]


@dataclass
class DownloadResult:
    path: Path
    size: int
    filename: str
    content_type: Optional[str]


ProgressCB = Callable[[int, int], Awaitable[None]]


def _build_connector(proxy: Optional[ProxyConfig]):
    if proxy is None:
        return None
    from aiohttp_socks import ProxyConnector

    return ProxyConnector.from_url(proxy.as_aiohttp_socks_url())


class Downloader:
    def __init__(self, proxy: Optional[ProxyConfig], user_agent: str, connections: int = 4):
        self.proxy = proxy
        self.user_agent = user_agent
        self.connections = max(1, connections)

    def _headers(self, extra: Optional[dict]) -> dict:
        headers = {"User-Agent": self.user_agent}
        if extra:
            headers.update(extra)
        return headers

    async def fetch_info(
        self, url: str, headers: Optional[dict] = None, cookies: Optional[dict] = None
    ) -> DownloadInfo:
        connector = _build_connector(self.proxy)
        timeout = ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, cookies=cookies) as session:
            hdrs = self._headers(headers)
            try:
                async with session.head(url, headers=hdrs, allow_redirects=True) as resp:
                    size = int(resp.headers.get("Content-Length") or 0)
                    accept_ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
                    content_type = resp.headers.get("Content-Type")
                    cd = resp.headers.get("Content-Disposition")
                    filename = resolve_filename(str(resp.url), cd, content_type)
                    if size > 0:
                        return DownloadInfo(size, accept_ranges, filename, content_type)
            except aiohttp.ClientError:
                pass

            probe_headers = dict(hdrs)
            probe_headers["Range"] = "bytes=0-0"
            async with session.get(url, headers=probe_headers, allow_redirects=True) as resp:
                content_type = resp.headers.get("Content-Type")
                cd = resp.headers.get("Content-Disposition")
                filename = resolve_filename(str(resp.url), cd, content_type)
                accept_ranges = resp.status == 206
                content_range = resp.headers.get("Content-Range")
                if content_range and "/" in content_range:
                    size = int(content_range.rsplit("/", 1)[-1])
                else:
                    size = int(resp.headers.get("Content-Length") or 0)
                resp.release()
                return DownloadInfo(size, accept_ranges, filename, content_type)

    async def download(
        self,
        url: str,
        dest_dir: Path,
        info: DownloadInfo,
        headers: Optional[dict] = None,
        cookies: Optional[dict] = None,
        max_size_bytes: Optional[int] = None,
        progress_cb: Optional[ProgressCB] = None,
        cancel_event: Optional[asyncio.Event] = None,
        pause_event: Optional[asyncio.Event] = None,
    ) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / info.filename
        state_path = dest_path.with_suffix(dest_path.suffix + ".state.json")

        if max_size_bytes and info.size and info.size > max_size_bytes:
            raise ValueError(f"File size {info.size} exceeds the configured limit {max_size_bytes}")

        free = free_disk_space(dest_dir)
        required = info.size or 0
        if required and free < required * 1.05:
            raise InsufficientDiskSpace(f"Need ~{required} bytes, only {free} free")

        use_segments = (
            info.accept_ranges
            and info.size >= MIN_SEGMENT_SIZE
            and self.connections > 1
        )

        if use_segments:
            await self._download_segmented(
                url, dest_path, state_path, info, headers, cookies, progress_cb, cancel_event, pause_event
            )
        else:
            await self._download_single(
                url, dest_path, state_path, info, headers, cookies, progress_cb, cancel_event, pause_event
            )

        state_path.unlink(missing_ok=True)
        size = dest_path.stat().st_size
        return DownloadResult(path=dest_path, size=size, filename=info.filename, content_type=info.content_type)

    async def _checkpoint(self) -> None:
        # Yield control so cancellation / pause can take effect between chunks.
        await asyncio.sleep(0)

    async def _wait_if_paused(self, pause_event: Optional[asyncio.Event]) -> None:
        if pause_event is not None:
            await pause_event.wait()

    def _check_cancel(self, cancel_event: Optional[asyncio.Event]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()

    async def _download_single(
        self,
        url: str,
        dest_path: Path,
        state_path: Path,
        info: DownloadInfo,
        headers: Optional[dict],
        cookies: Optional[dict],
        progress_cb: Optional[ProgressCB],
        cancel_event: Optional[asyncio.Event],
        pause_event: Optional[asyncio.Event],
    ) -> None:
        existing = dest_path.stat().st_size if dest_path.exists() else 0
        resume_supported = info.accept_ranges and existing > 0
        downloaded = existing if resume_supported else 0

        connector = _build_connector(self.proxy)
        timeout = ClientTimeout(total=None, sock_connect=30, sock_read=60)

        attempt = 0
        while True:
            try:
                hdrs = self._headers(headers)
                mode = "ab" if resume_supported and downloaded else "wb"
                if resume_supported and downloaded:
                    hdrs["Range"] = f"bytes={downloaded}-"

                async with aiohttp.ClientSession(connector=connector, timeout=timeout, cookies=cookies) as session:
                    async with session.get(url, headers=hdrs, allow_redirects=True) as resp:
                        if resp.status not in (200, 206):
                            raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status)
                        with open(dest_path, mode) as fh:
                            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                                self._check_cancel(cancel_event)
                                await self._wait_if_paused(pause_event)
                                fh.write(chunk)
                                downloaded += len(chunk)
                                if progress_cb:
                                    await progress_cb(downloaded, info.size or downloaded)
                                await self._checkpoint()
                return
            except DownloadCancelled:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as exc:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise
                wait = BACKOFF_BASE**attempt
                logger.warning("Download error (%s), retry %s/%s in %.1fs", exc, attempt, MAX_RETRIES, wait)
                await asyncio.sleep(wait)
                downloaded = dest_path.stat().st_size if dest_path.exists() else 0
                resume_supported = info.accept_ranges and downloaded > 0

    async def _download_segmented(
        self,
        url: str,
        dest_path: Path,
        state_path: Path,
        info: DownloadInfo,
        headers: Optional[dict],
        cookies: Optional[dict],
        progress_cb: Optional[ProgressCB],
        cancel_event: Optional[asyncio.Event],
        pause_event: Optional[asyncio.Event],
    ) -> None:
        segments = self._load_or_init_segments(dest_path, state_path, info.size)

        if not dest_path.exists() or dest_path.stat().st_size != info.size:
            with open(dest_path, "wb") as fh:
                fh.truncate(info.size)

        progress_lock = asyncio.Lock()
        total_done = sum(s["done"] for s in segments)

        async def report(delta: int) -> None:
            nonlocal total_done
            async with progress_lock:
                total_done += delta
                if progress_cb:
                    await progress_cb(total_done, info.size)

        def save_state() -> None:
            state_path.write_text(json.dumps({"size": info.size, "segments": segments}))

        async def run_segment(seg: dict) -> None:
            start, end = seg["start"], seg["end"]
            done = seg["done"]
            if start + done > end:
                return
            connector = _build_connector(self.proxy)
            timeout = ClientTimeout(total=None, sock_connect=30, sock_read=60)
            attempt = 0
            fd = os.open(dest_path, os.O_WRONLY)
            try:
                while start + done <= end:
                    try:
                        hdrs = self._headers(headers)
                        hdrs["Range"] = f"bytes={start + done}-{end}"
                        async with aiohttp.ClientSession(
                            connector=connector, timeout=timeout, cookies=cookies
                        ) as session:
                            async with session.get(url, headers=hdrs, allow_redirects=True) as resp:
                                if resp.status != 206:
                                    raise aiohttp.ClientResponseError(
                                        resp.request_info, resp.history, status=resp.status
                                    )
                                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                                    self._check_cancel(cancel_event)
                                    await self._wait_if_paused(pause_event)
                                    offset = start + done
                                    await asyncio.get_running_loop().run_in_executor(
                                        None, os.pwrite, fd, chunk, offset
                                    )
                                    done += len(chunk)
                                    seg["done"] = done
                                    await report(len(chunk))
                                    await self._checkpoint()
                        return
                    except DownloadCancelled:
                        raise
                    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as exc:
                        attempt += 1
                        if attempt > MAX_RETRIES:
                            raise
                        wait = BACKOFF_BASE**attempt
                        logger.warning(
                            "Segment %s-%s error (%s), retry %s/%s in %.1fs",
                            start,
                            end,
                            exc,
                            attempt,
                            MAX_RETRIES,
                            wait,
                        )
                        await asyncio.sleep(wait)
            finally:
                os.close(fd)

        save_state()
        state_saver = asyncio.create_task(self._periodic_save(save_state, cancel_event))
        try:
            await asyncio.gather(*(run_segment(seg) for seg in segments if seg["done"] < (seg["end"] - seg["start"] + 1)))
        finally:
            state_saver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state_saver

    async def _periodic_save(self, save_fn: Callable[[], None], cancel_event: Optional[asyncio.Event]) -> None:
        try:
            while True:
                await asyncio.sleep(2.0)
                save_fn()
        except asyncio.CancelledError:
            save_fn()
            raise

    def _load_or_init_segments(self, dest_path: Path, state_path: Path, size: int) -> list[dict]:
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text())
                if data.get("size") == size:
                    return data["segments"]
            except (json.JSONDecodeError, KeyError):
                pass

        n = self.connections
        base = size // n
        segments = []
        start = 0
        for i in range(n):
            end = start + base - 1 if i < n - 1 else size - 1
            segments.append({"start": start, "end": end, "done": 0})
            start = end + 1
        return segments
