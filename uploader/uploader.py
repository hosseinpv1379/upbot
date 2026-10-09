"""Parallel ("FastTelethon"-style) upload, media attributes, and large-file splitting.

The parallel uploader opens several additional MTProto connections that share the
client's existing auth key (the same technique as the well-known community
"FastTelethon" snippet) so that large files upload several times faster than
Telethon's single-connection ``send_file``. If anything about this low-level path
breaks (e.g. a future Telethon release changes its private API), every call falls
back transparently to plain ``client.upload_file`` / ``client.send_file``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
from pathlib import Path
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient, helpers, utils
from telethon.network import MTProtoSender
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputFile,
    InputFileBig,
)

from uploader import media

logger = logging.getLogger(__name__)

ProgressCB = Callable[[int, int], Awaitable[None]]

BIG_FILE_THRESHOLD = 10 * 1024 * 1024
MAX_PHOTO_SIZE = 10 * 1024 * 1024
ALBUM_BATCH_SIZE = 10


class _PartSender:
    """Owns one extra MTProto connection and feeds it every `stride`-th file part."""

    def __init__(self, sender: MTProtoSender, file_id: int, part_count: int, big_file: bool, index: int, stride: int):
        self.sender = sender
        self.file_id = file_id
        self.part_count = part_count
        self.big_file = big_file
        self.index = index
        self.stride = stride
        self._previous: Optional[asyncio.Future] = None

    async def send(self, data: bytes) -> None:
        if self._previous is not None:
            await self._previous
        if self.big_file:
            request = SaveBigFilePartRequest(self.file_id, self.index, self.part_count, data)
        else:
            request = SaveFilePartRequest(self.file_id, self.index, data)
        self._previous = asyncio.ensure_future(self.sender.send(request))
        self.index += self.stride

    async def disconnect(self) -> None:
        if self._previous is not None:
            await self._previous
        await self.sender.disconnect()


class ParallelUploader:
    def __init__(self, client: TelegramClient, connections: int):
        self.client = client
        self.connections = max(1, connections)
        self._senders: list[_PartSender] = []
        self._ticker = 0

    async def _open_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.client.session.dc_id)
        sender = MTProtoSender(self.client.session.auth_key, loggers=self.client._log)
        await sender.connect(
            self.client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=self.client._log,
                proxy=self.client._proxy,
            )
        )
        return sender

    async def start(self, file_id: int, part_count: int, big_file: bool) -> None:
        connections = min(self.connections, max(part_count, 1))
        raw_senders = await asyncio.gather(*(self._open_sender() for _ in range(connections)))
        self._senders = [
            _PartSender(raw, file_id, part_count, big_file, i, connections)
            for i, raw in enumerate(raw_senders)
        ]

    async def send_part(self, data: bytes) -> None:
        sender = self._senders[self._ticker]
        self._ticker = (self._ticker + 1) % len(self._senders)
        await sender.send(data)

    async def finish(self) -> None:
        await asyncio.gather(*(s.disconnect() for s in self._senders), return_exceptions=True)
        self._senders = []


async def _upload_file_fast(
    client: TelegramClient,
    file_path: Path,
    connections: int,
    progress_cb: Optional[ProgressCB],
    cancel_event: Optional[asyncio.Event],
):
    file_size = file_path.stat().st_size
    part_size = int(utils.get_appropriated_part_size(file_size) * 1024)
    part_count = math.ceil(file_size / part_size)
    is_big = file_size > BIG_FILE_THRESHOLD
    file_id = helpers.generate_random_long()

    pool = ParallelUploader(client, connections)
    await pool.start(file_id, part_count, is_big)

    loop = asyncio.get_running_loop()
    uploaded = 0
    try:
        with open(file_path, "rb") as fh:
            for _ in range(part_count):
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError("upload cancelled")
                chunk = await loop.run_in_executor(None, fh.read, part_size)
                await pool.send_part(chunk)
                uploaded += len(chunk)
                if progress_cb:
                    await progress_cb(uploaded, file_size)
    finally:
        await pool.finish()

    if is_big:
        return InputFileBig(id=file_id, parts=part_count, name=file_path.name)
    return InputFile(id=file_id, parts=part_count, name=file_path.name, md5_checksum="")


async def upload_file(
    client: TelegramClient,
    file_path: Path,
    connections: int,
    progress_cb: Optional[ProgressCB] = None,
    cancel_event: Optional[asyncio.Event] = None,
):
    """Upload a local file and return a Telethon ``InputFile``/``InputFileBig``.

    Tries the parallel fast path first; falls back to Telethon's own (slower but
    battle-tested) single-connection uploader on any failure.
    """
    try:
        return await _upload_file_fast(client, file_path, connections, progress_cb, cancel_event)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - any internal-API breakage falls back safely
        logger.exception("Fast parallel upload failed for %s, falling back to client.upload_file", file_path)
        return await client.upload_file(str(file_path), progress_callback=progress_cb)


async def send_media(
    client: TelegramClient,
    chat_id,
    file_path: Path,
    caption: str,
    parse_mode: Optional[str],
    force_document: bool,
    auto_thumbnail: bool,
    connections: int,
    progress_cb: Optional[ProgressCB] = None,
    cancel_event: Optional[asyncio.Event] = None,
):
    size = file_path.stat().st_size
    kind = media.classify_extension(file_path.suffix.lower())

    if kind == "image" and size <= MAX_PHOTO_SIZE and not force_document:
        return await client.send_file(
            chat_id,
            str(file_path),
            caption=caption,
            parse_mode=parse_mode,
            progress_callback=progress_cb,
        )

    input_file = await upload_file(client, file_path, connections, progress_cb, cancel_event)

    attributes = [DocumentAttributeFilename(file_name=file_path.name)]
    thumb_path: Optional[Path] = None
    supports_streaming = False

    if kind == "video" and not force_document:
        info = await media.probe_video(file_path)
        attributes.append(
            DocumentAttributeVideo(
                duration=int(info.duration), w=info.width or 1, h=info.height or 1, supports_streaming=True
            )
        )
        supports_streaming = True
        if auto_thumbnail:
            thumb_path = await media.generate_thumbnail(file_path, info.duration)
    elif kind == "audio" and not force_document:
        info = await media.probe_audio(file_path)
        attributes.append(
            DocumentAttributeAudio(
                duration=int(info.duration),
                title=info.title or file_path.stem,
                performer=info.performer or "Unknown",
            )
        )

    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    try:
        message = await client.send_file(
            chat_id,
            input_file,
            caption=caption,
            parse_mode=parse_mode,
            attributes=attributes,
            force_document=True,
            mime_type=mime_type,
            thumb=str(thumb_path) if thumb_path else None,
            supports_streaming=supports_streaming,
        )
    finally:
        if thumb_path and thumb_path.exists():
            thumb_path.unlink(missing_ok=True)
    return message


async def split_archive_7z(file_path: Path, part_size: int) -> list[Path]:
    """Split any file into password-less 7z volumes (`name.7z.001`, `.002`, ...)."""
    out_dir = file_path.parent
    archive_path = out_dir / f"{file_path.name}.7z"
    cmd = ["7z", "a", f"-v{part_size}b", str(archive_path), str(file_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"7z split failed: {stderr.decode(errors='ignore')}")
    return sorted(out_dir.glob(f"{archive_path.name}.*"))


async def split_for_limit(file_path: Path, part_size: int) -> list[Path]:
    """Split a file so every part fits under `part_size` bytes.

    Videos are re-segmented with ffmpeg (so every part is itself a playable video);
    everything else is split into 7z volumes.
    """
    kind = media.classify_extension(file_path.suffix.lower())
    if kind == "video":
        info = await media.probe_video(file_path)
        if info.duration > 0:
            bytes_per_sec = file_path.stat().st_size / info.duration
            segment_seconds = max(int(part_size / max(bytes_per_sec, 1)), 10)
            parts = await media.split_video_ffmpeg(file_path, segment_seconds)
            if parts:
                return parts
    return await split_archive_7z(file_path, part_size)


async def send_parts_as_album(
    client: TelegramClient,
    chat_id,
    parts: list[Path],
    caption: str,
    parse_mode: Optional[str],
) -> list:
    """Upload split parts, grouped into albums of up to 10 (Telegram's media-group limit)."""
    messages: list = []
    for i in range(0, len(parts), ALBUM_BATCH_SIZE):
        batch = [str(p) for p in parts[i : i + ALBUM_BATCH_SIZE]]
        batch_caption = caption if i == 0 else ""
        result = await client.send_file(
            chat_id, batch, caption=batch_caption, parse_mode=parse_mode, force_document=True
        )
        messages.extend(result if isinstance(result, list) else [result])
    return messages
