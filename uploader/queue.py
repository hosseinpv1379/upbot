"""Persistent job queue: download -> upload pipeline with bounded concurrency."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from telethon import Button

from uploader import uploader as uploader_mod
from uploader.context import AppContext
from uploader.db import Job
from uploader.downloader import (
    Downloader,
    DownloadCancelled,
    DownloadInfo,
    InsufficientDiskSpace,
)
from uploader.progress import ProgressTracker
from uploader.utils import (
    CaptionContext,
    human_duration,
    human_size,
    now_stamp,
    render_caption,
    sha256_partial,
    sha256_url,
    truncate_caption,
)

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL = 3600.0
ORPHAN_MAX_AGE = 6 * 3600.0


def job_buttons(job_id: int, paused: bool) -> list:
    pause_label = "▶ ادامه" if paused else "⏸ توقف"
    return [[Button.inline("❌ لغو", data=f"cancel:{job_id}".encode()), Button.inline(pause_label, data=f"pause:{job_id}".encode())]]


def cancel_only_button(job_id: int) -> list:
    return [[Button.inline("❌ لغو", data=f"cancel:{job_id}".encode())]]


def build_message_link(chat_id: int, message_id: int, username: Optional[str]) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    internal = str(chat_id)
    if internal.startswith("-100"):
        internal = internal[4:]
    elif internal.startswith("-"):
        internal = internal[1:]
    return f"https://t.me/c/{internal}/{message_id}"


class JobManager:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.download_sem = asyncio.Semaphore(ctx.settings.download_concurrency)
        self.upload_sem = asyncio.Semaphore(ctx.settings.upload_concurrency)
        self._queue: "asyncio.Queue[int]" = asyncio.Queue()
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancel_events: dict[int, asyncio.Event] = {}
        self._pause_events: dict[int, asyncio.Event] = {}
        self._dispatcher: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._status_messages: dict[int, object] = {}
        self._batch_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        restarted = await self.ctx.db.reset_stuck_jobs()
        if restarted:
            logger.info("Re-queued %s job(s) interrupted by a restart", restarted)
        for job in await self.ctx.db.list_jobs(statuses=("queued",), limit=1000):
            await self._queue.put(job.id)
        self._dispatcher = asyncio.create_task(self._dispatch_loop())
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())

        # A batch whose members all finished downloading right before a crash would
        # otherwise never resume, since no running job is left to trigger it again.
        stranded_batches = {
            j.batch_id for j in await self.ctx.db.list_jobs(statuses=("downloaded",), limit=1000) if j.batch_id
        }
        for batch_id in stranded_batches:
            asyncio.create_task(self._maybe_finish_batch(batch_id))

    async def shutdown(self) -> None:
        if self._dispatcher:
            self._dispatcher.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for event in self._cancel_events.values():
            event.set()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _periodic_cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(CLEANUP_INTERVAL)
                await self._cleanup_orphaned_files()
        except asyncio.CancelledError:
            raise

    async def _cleanup_orphaned_files(self) -> None:
        download_dir = self.ctx.settings.download_dir
        if not download_dir.exists():
            return
        active_jobs = await self.ctx.db.list_jobs(limit=1000)
        active_paths = {Path(j.file_path).resolve() for j in active_jobs if j.file_path and j.status not in ("done",)}
        now = time.time()
        for path in download_dir.iterdir():
            if not path.is_file():
                continue
            try:
                if path.resolve() in active_paths:
                    continue
                age = now - path.stat().st_mtime
                if age > ORPHAN_MAX_AGE:
                    path.unlink(missing_ok=True)
                    logger.info("Removed orphaned temp file %s (age %.0fs)", path, age)
            except OSError:
                logger.exception("Failed to inspect/remove %s during cleanup", path)

    async def enqueue(
        self,
        admin_chat_id: int,
        url: str,
        caption: str,
        channel_ids: list[int],
        options: Optional[dict] = None,
        batch_id: Optional[str] = None,
        batch_index: Optional[int] = None,
    ) -> int:
        url_hash = sha256_url(url)
        job_id = await self.ctx.db.create_job(
            admin_chat_id, url, caption, channel_ids, url_hash, options or {},
            batch_id=batch_id, batch_index=batch_index,
        )
        await self._queue.put(job_id)
        return job_id

    def cancel_job(self, job_id: int) -> bool:
        event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def toggle_pause(self, job_id: int) -> Optional[bool]:
        """Returns the new paused state, or None if the job isn't currently downloading."""
        event = self._pause_events.get(job_id)
        if event is None:
            return None
        if event.is_set():
            event.clear()
            return True
        event.set()
        return False

    async def retry(self, job_id: int) -> bool:
        job = await self.ctx.db.get_job(job_id)
        if job is None or job.status not in ("failed", "cancelled"):
            return False
        await self.ctx.db.update_job(job_id, status="queued", error=None)
        await self._queue.put(job_id)
        return True

    async def _dispatch_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            task = asyncio.create_task(self._run_job(job_id))
            self._tasks[job_id] = task
            task.add_done_callback(lambda t, jid=job_id: self._tasks.pop(jid, None))

    async def _edit_status(self, job: Job, text: str, buttons=None) -> None:
        msg = self._status_messages.get(job.id)
        try:
            if msg is None:
                msg = await self.ctx.bot.send_message(job.admin_chat_id, text, buttons=buttons)
                self._status_messages[job.id] = msg
                await self.ctx.db.update_job(job.id, status_message_id=msg.id)
            else:
                await msg.edit(text, buttons=buttons)
        except Exception:  # noqa: BLE001 - message editing must never crash the pipeline
            logger.exception("Failed to update status message for job %s", job.id)

    async def _run_job(self, job_id: int) -> None:
        job = await self.ctx.db.get_job(job_id)
        if job is None or job.status in ("done", "cancelled"):
            return

        batch_id = job.batch_id
        cancel_event = asyncio.Event()
        pause_event = asyncio.Event()
        pause_event.set()
        self._cancel_events[job_id] = cancel_event
        self._pause_events[job_id] = pause_event

        try:
            duplicate = await self._check_duplicate_before_download(job)
            if duplicate:
                return

            file_path = await self._process_download(job, cancel_event, pause_event)
            job = await self.ctx.db.get_job(job_id)  # refresh after download

            if batch_id:
                # Uploaded together with the rest of the batch once every sibling is
                # ready — see _maybe_finish_batch, triggered from the finally below.
                await self.ctx.db.update_job(job_id, status="downloaded")
                await self._edit_status(
                    job, f"✅ دانلود شد: {job.filename or job.url}\nدر انتظار بقیه‌ی فایل‌های گروه برای آپلود دسته‌جمعی..."
                )
                return

            await self._process_upload(job, file_path, cancel_event)
            await self.ctx.db.update_job(job_id, status="done")
            await self._edit_status(job, f"✅ {job.filename or job.url}\nبا موفقیت در همه‌ی کانال‌ها ارسال شد.")
        except DownloadCancelled:
            await self.ctx.db.update_job(job_id, status="cancelled")
            await self._edit_status(job, "❌ لغو شد.")
        except asyncio.CancelledError:
            await self.ctx.db.update_job(job_id, status="cancelled", error="cancelled")
            raise
        except InsufficientDiskSpace as exc:
            await self._fail(job, f"فضای دیسک کافی نیست: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s failed", job_id)
            await self._fail(job, str(exc))
        finally:
            self._cancel_events.pop(job_id, None)
            self._pause_events.pop(job_id, None)
            self._status_messages.pop(job_id, None)
            if batch_id:
                await self._maybe_finish_batch(batch_id)
            else:
                await self._cleanup_files(job)

    async def _fail(self, job: Job, message: str) -> None:
        await self.ctx.db.update_job(job.id, status="failed", error=message)
        await self._edit_status(job, f"⛔️ خطا در کار شماره {job.id}:\n{message}")

    async def _cleanup_files(self, job: Job) -> None:
        job = await self.ctx.db.get_job(job.id)
        if job and job.file_path:
            path = Path(job.file_path)
            path.unlink(missing_ok=True)
            state = path.with_suffix(path.suffix + ".state.json")
            state.unlink(missing_ok=True)

    async def _maybe_finish_batch(self, batch_id: str) -> None:
        """Called whenever any job in a batch finishes its download phase (success,
        failure, cancellation, or duplicate-skip). Once every sibling has reached a
        terminal download-phase state, uploads whatever downloaded successfully as a
        single Telegram album (media group) with one shared caption."""
        lock = self._batch_locks.setdefault(batch_id, asyncio.Lock())
        async with lock:
            siblings = await self.ctx.db.list_jobs_by_batch(batch_id)
            if any(s.status in ("queued", "downloading", "paused") for s in siblings):
                return  # other members of the batch are still downloading
            self._batch_locks.pop(batch_id, None)
            ready = [s for s in siblings if s.status == "downloaded"]
            if not ready:
                return  # every member failed/cancelled/was a duplicate — nothing to send
            await self._upload_batch(batch_id, ready)

    async def _upload_batch(self, batch_id: str, jobs: list[Job]) -> None:
        admin_chat_id = jobs[0].admin_chat_id
        channels = await self.ctx.db.list_channels()
        channel_ids = jobs[0].channel_ids or (
            [c.chat_id for c in channels if c.is_default] or [c.chat_id for c in channels[:1]]
        )
        file_paths = [Path(j.file_path) for j in jobs if j.file_path]

        status_msg = await self.ctx.bot.send_message(
            admin_chat_id, f"📦 در حال آپلود گروهی {len(jobs)} فایل در یک آلبوم..."
        )

        if not channel_ids:
            await status_msg.edit("⛔️ هیچ کانالی تنظیم نشده است؛ آپلود گروهی لغو شد.")
            for job in jobs:
                await self.ctx.db.update_job(job.id, status="failed", error="no channel configured")
            self._cleanup_batch_files(jobs)
            return

        try:
            for chat_id in channel_ids:
                channel = next((c for c in channels if c.chat_id == chat_id), None)
                caption = await self._render_caption_for(jobs[0], file_paths[0], channel, 1, len(jobs))
                messages = await uploader_mod.send_parts_as_album(
                    self.ctx.upload_client, chat_id, file_paths, caption, "html"
                )
                for job, message in zip(jobs, messages):
                    if message is not None:
                        await self.ctx.db.record_upload(
                            job.id, job.url, Path(job.file_path).name, job.file_size,
                            chat_id, message.id, job.file_hash, job.url_hash,
                        )

            for job in jobs:
                await self.ctx.db.update_job(job.id, status="done")
            await status_msg.edit(f"✅ {len(jobs)} فایل با موفقیت در قالب یک آلبوم ارسال شد.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Batch %s upload failed", batch_id)
            for job in jobs:
                await self.ctx.db.update_job(job.id, status="failed", error=str(exc))
            await status_msg.edit(f"⛔️ خطا در آپلود گروهی: {exc}")
        finally:
            self._cleanup_batch_files(jobs)

    def _cleanup_batch_files(self, jobs: list[Job]) -> None:
        for job in jobs:
            if not job.file_path:
                continue
            path = Path(job.file_path)
            path.unlink(missing_ok=True)
            state = path.with_suffix(path.suffix + ".state.json")
            state.unlink(missing_ok=True)

    async def _check_duplicate_before_download(self, job: Job) -> bool:
        dup = await self.ctx.db.find_duplicate(job.url_hash, None)
        if not dup:
            return False
        channel = next((c for c in await self.ctx.db.list_channels() if c.chat_id == dup["channel_id"]), None)
        username = channel.username if channel else None
        link = build_message_link(dup["channel_id"], dup["message_id"], username)
        await self.ctx.db.update_job(job.id, status="done")
        await self._edit_status(
            job,
            f"⚠️ این لینک قبلاً آپلود شده است.\n{dup['filename']}\n{link}",
        )
        return True

    async def _process_download(
        self, job: Job, cancel_event: asyncio.Event, pause_event: asyncio.Event
    ) -> Path:
        await self.ctx.db.update_job(job.id, status="downloading")
        settings = self.ctx.settings

        async with self.download_sem:
            downloader = Downloader(
                settings.dl_proxy_config, settings.user_agent, settings.segmented_download_connections
            )
            headers = job.options.get("headers")
            cookies = job.options.get("cookies")
            info: DownloadInfo = await downloader.fetch_info(job.url, headers=headers, cookies=cookies)
            await self.ctx.db.update_job(job.id, filename=info.filename, file_size=info.size)

            tracker = ProgressTracker(
                sink=lambda text: self._edit_status(job, text, job_buttons(job.id, not pause_event.is_set())),
                title=info.filename,
                phase="download",
                interval=settings.progress_update_interval,
            )

            async def progress_cb(done: int, total: int) -> None:
                await tracker.update(done, total)

            result = await downloader.download(
                job.url,
                settings.download_dir,
                info,
                headers=headers,
                cookies=cookies,
                max_size_bytes=settings.max_file_size_bytes,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
                pause_event=pause_event,
            )
            await tracker.update(result.size, result.size, force=True)
            await self.ctx.db.update_job(job.id, file_path=str(result.path), file_size=result.size)
            return result.path

    async def _process_upload(self, job: Job, file_path: Path, cancel_event: asyncio.Event) -> None:
        await self.ctx.db.update_job(job.id, status="uploading")
        settings = self.ctx.settings
        db = self.ctx.db

        loop = asyncio.get_running_loop()
        file_hash = await loop.run_in_executor(None, sha256_partial, file_path, file_path.stat().st_size)
        await db.update_job(job.id, file_hash=file_hash)
        job.file_hash = file_hash

        channels = await db.list_channels()
        channel_ids = job.channel_ids or ([c.chat_id for c in channels if c.is_default] or [c.chat_id for c in channels[:1]])
        if not channel_ids:
            raise RuntimeError("هیچ کانالی تنظیم نشده است؛ با /addchannel یک کانال اضافه کنید.")

        force_document = job.options.get("force_document", (await db.get_setting("force_document")) == "1")
        auto_thumbnail = job.options.get("auto_thumbnail", (await db.get_setting("auto_thumbnail")) == "1")
        split_mode = (await db.get_setting("split_mode")) == "1"
        connections = settings.fast_upload_connections

        async with self.upload_sem:
            size = file_path.stat().st_size
            parts: Optional[list[Path]] = None
            if size > self.ctx.upload_limit_bytes:
                if not split_mode:
                    raise RuntimeError(
                        f"حجم فایل ({human_size(size)}) از سقف آپلود تلگرام بیشتر است و حالت تقسیم فایل خاموش است."
                    )
                margin = int(self.ctx.upload_limit_bytes * 0.95)
                parts = await uploader_mod.split_for_limit(file_path, margin)

            for index, chat_id in enumerate(channel_ids):
                channel = next((c for c in channels if c.chat_id == chat_id), None)
                caption = await self._render_caption_for(job, file_path, channel, index + 1, len(channel_ids))

                tracker = ProgressTracker(
                    sink=lambda text: self._edit_status(job, text, cancel_only_button(job.id)),
                    title=file_path.name,
                    phase="upload",
                    interval=settings.progress_update_interval,
                )

                async def progress_cb(done: int, total: int) -> None:
                    await tracker.update(done, total)

                if parts:
                    messages = await uploader_mod.send_parts_as_album(
                        self.ctx.upload_client, chat_id, parts, caption, "html"
                    )
                    message = messages[0] if messages else None
                else:
                    message = await uploader_mod.send_media(
                        self.ctx.upload_client,
                        chat_id,
                        file_path,
                        caption,
                        "html",
                        force_document,
                        auto_thumbnail,
                        connections,
                        progress_cb,
                        cancel_event,
                    )

                if message is not None:
                    await db.record_upload(
                        job.id, job.url, file_path.name, size, chat_id, message.id, job.file_hash, job.url_hash
                    )

            if parts:
                for part in parts:
                    Path(part).unlink(missing_ok=True)

    async def _render_caption_for(self, job: Job, file_path: Path, channel, index: int, total: int) -> str:
        from uploader import media

        kind = media.classify_extension(file_path.suffix.lower())
        duration = ""
        resolution = ""
        if kind == "video":
            info = await media.probe_video(file_path)
            duration = human_duration(info.duration)
            resolution = f"{info.width}x{info.height}" if info.width else ""
        elif kind == "audio":
            info = await media.probe_audio(file_path)
            duration = human_duration(info.duration)

        context = CaptionContext(
            filename=file_path.stem,
            size=human_size(file_path.stat().st_size),
            duration=duration,
            resolution=resolution,
            ext=file_path.suffix.lstrip("."),
            date=now_stamp(),
            index=f"{index}/{total}" if total > 1 else "",
        )

        global_template = await self.ctx.db.get_setting("default_caption_template", "{filename}")
        global_footer = await self.ctx.db.get_setting("default_footer", "")
        template = job.caption or (channel.caption_template if channel else "") or global_template
        footer = (channel.footer if channel else "") or global_footer
        limit = self.ctx.settings.caption_limit_default
        if self.ctx.user_client is not None:
            limit = self.ctx.settings.caption_limit_premium
        rendered = render_caption(template, context, footer)
        return truncate_caption(rendered, limit)
