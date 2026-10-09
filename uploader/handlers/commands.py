"""Admin slash-commands."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from telethon import Button, events

from uploader.context import AppContext
from uploader.utils import free_disk_space, human_duration, human_size

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 ربات آپلودر تلگرام\n\n"
    "یک یا چند لینک دانلود مستقیم بفرستید (هر لینک در یک خط)، به صورت اختیاری"
    " بعد از یک خط `---` کپشن دلخواه را اضافه کنید.\n\n"
    "برای افزودن کانال با آیدی عددی خام (مثل -100...) که بات قبلاً نمی‌شناسد،"
    " به‌جای /addchannel یک پیام از آن کانال را برای بات فوروارد کنید.\n\n"
    "برای کار با دکمه به‌جای تایپ دستور، از /menu استفاده کنید — همه‌ی موارد زیر"
    " (صف، کانال‌ها، تاریخچه، آمار، تنظیمات، پراکسی) از همان منو هم در دسترس‌اند.\n\n"
    "دستورات:\n"
    "/queue — نمایش صف کارها\n"
    "/cancel <id> — لغو یک کار\n"
    "/retry <id> — تلاش دوباره برای کار ناموفق/لغو شده\n"
    "/history [n] — آخرین آپلودها\n"
    "/stats — آمار کلی ربات\n"
    "/channels — نمایش کانال‌های تنظیم‌شده\n"
    "/addchannel <id|@username> — افزودن کانال\n"
    "/delchannel <id|@username> — حذف کانال\n"
    "/setdefault <id|@username> — تعیین کانال پیش‌فرض\n"
    "/setcaption [کانال] <قالب> — تعیین قالب کپشن\n"
    "/setfooter [کانال] <متن> — تعیین پابرگ کپشن\n"
    "/settings — تنظیمات سریع\n"
    "/proxy — وضعیت و تست پراکسی\n"
)


def register(ctx: AppContext) -> None:
    bot = ctx.bot

    def admin_only(func):
        async def wrapper(event: events.NewMessage.Event) -> None:
            if not ctx.is_admin(event.sender_id):
                return
            await func(event)

        return wrapper

    @bot.on(events.NewMessage(pattern=r"^/start$"))
    @admin_only
    async def start_cmd(event: events.NewMessage.Event) -> None:
        from uploader.handlers import menu as menu_mod

        text, buttons = await menu_mod.render_main(ctx)
        await event.reply(text, buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"^/menu$"))
    @admin_only
    async def menu_cmd(event: events.NewMessage.Event) -> None:
        from uploader.handlers import menu as menu_mod

        text, buttons = await menu_mod.render_main(ctx)
        await event.reply(text, buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"^/help$"))
    @admin_only
    async def help_cmd(event: events.NewMessage.Event) -> None:
        await event.reply(HELP_TEXT)

    @bot.on(events.NewMessage(pattern=r"^/queue$"))
    @admin_only
    async def queue_cmd(event: events.NewMessage.Event) -> None:
        jobs = await ctx.db.list_jobs(statuses=("queued", "downloading", "uploading", "paused"), limit=30)
        if not jobs:
            await event.reply("صف خالی است.")
            return
        lines = []
        for job in jobs:
            status_fa = {
                "queued": "در صف",
                "downloading": "در حال دانلود",
                "uploading": "در حال آپلود",
                "paused": "متوقف‌شده",
            }.get(job.status, job.status)
            lines.append(f"#{job.id} — {job.filename or job.url} — {status_fa}")
        await event.reply("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"^/cancel (\d+)$"))
    @admin_only
    async def cancel_cmd(event: events.NewMessage.Event) -> None:
        job_id = int(event.pattern_match.group(1))
        ok = ctx.job_manager.cancel_job(job_id)
        await event.reply("درخواست لغو ارسال شد." if ok else "این کار یافت نشد یا فعال نیست.")

    @bot.on(events.NewMessage(pattern=r"^/retry (\d+)$"))
    @admin_only
    async def retry_cmd(event: events.NewMessage.Event) -> None:
        job_id = int(event.pattern_match.group(1))
        ok = await ctx.job_manager.retry(job_id)
        await event.reply("کار دوباره به صف اضافه شد." if ok else "این کار قابل تلاش دوباره نیست.")

    @bot.on(events.NewMessage(pattern=r"^/history(?:\s+(\d+))?$"))
    @admin_only
    async def history_cmd(event: events.NewMessage.Event) -> None:
        n = int(event.pattern_match.group(1) or 10)
        rows = await ctx.db.list_history(limit=n)
        if not rows:
            await event.reply("هنوز آپلودی ثبت نشده است.")
            return
        from uploader.queue import build_message_link

        channels = await ctx.db.list_channels()
        lines = []
        for row in rows:
            channel = next((c for c in channels if c.chat_id == row["channel_id"]), None)
            link = build_message_link(row["channel_id"], row["message_id"], channel.username if channel else None)
            lines.append(f"{row['filename']} ({human_size(row['file_size'])}) — {link}")
        await event.reply("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"^/stats$"))
    @admin_only
    async def stats_cmd(event: events.NewMessage.Event) -> None:
        stats = await ctx.db.stats()
        free = free_disk_space(ctx.settings.download_dir)
        proxy = ctx.settings.tg_proxy_config
        proxy_text = f"{proxy.scheme}://{proxy.host}:{proxy.port}" if proxy else "غیرفعال"
        by_status = ", ".join(f"{k}: {v}" for k, v in stats["jobs_by_status"].items()) or "—"
        text = (
            f"📊 آمار ربات\n"
            f"تعداد آپلودها: {stats['uploads']}\n"
            f"حجم کل: {human_size(stats['total_bytes'])}\n"
            f"وضعیت کارها: {by_status}\n"
            f"فضای آزاد دیسک: {human_size(free)}\n"
            f"زمان روشن بودن: {human_duration(ctx.uptime_seconds)}\n"
            f"پراکسی: {proxy_text}"
        )
        await event.reply(text)

    @bot.on(events.NewMessage(pattern=r"^/channels$"))
    @admin_only
    async def channels_cmd(event: events.NewMessage.Event) -> None:
        channels = await ctx.db.list_channels()
        if not channels:
            await event.reply("هیچ کانالی تنظیم نشده است.")
            return
        lines = []
        for c in channels:
            mark = " (پیش‌فرض)" if c.is_default else ""
            lines.append(f"{c.chat_id} — {c.title or c.username}{mark}")
        await event.reply("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"^/addchannel\s+(.+)$"))
    @admin_only
    async def addchannel_cmd(event: events.NewMessage.Event) -> None:
        target = event.pattern_match.group(1).strip()
        try:
            entity = await bot.get_entity(target)
        except Exception as exc:  # noqa: BLE001
            await event.reply(
                f"کانال پیدا نشد: {exc}\n\n"
                "اگر آیدی عددی خام دادید و بات قبلاً این کانال را نمی‌شناسد، به‌جای این دستور"
                " یک پیام از آن کانال را برای بات فوروارد کنید."
            )
            return
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)
        username = getattr(entity, "username", None)
        from telethon.utils import get_peer_id

        chat_id = get_peer_id(entity)
        await ctx.db.add_channel(chat_id, title, username)
        await event.reply(f"✅ کانال «{title}» اضافه شد.")

    @bot.on(events.NewMessage(pattern=r"^/delchannel\s+(.+)$"))
    @admin_only
    async def delchannel_cmd(event: events.NewMessage.Event) -> None:
        target = event.pattern_match.group(1).strip()
        chat_id = await _resolve_channel_id(ctx, bot, target)
        if chat_id is None:
            await event.reply("کانال پیدا نشد.")
            return
        await ctx.db.remove_channel(chat_id)
        await event.reply("✅ حذف شد.")

    @bot.on(events.NewMessage(pattern=r"^/setdefault\s+(.+)$"))
    @admin_only
    async def setdefault_cmd(event: events.NewMessage.Event) -> None:
        target = event.pattern_match.group(1).strip()
        chat_id = await _resolve_channel_id(ctx, bot, target)
        if chat_id is None:
            await event.reply("کانال پیدا نشد.")
            return
        await ctx.db.set_default_channel(chat_id)
        await event.reply("✅ کانال پیش‌فرض تنظیم شد.")

    @bot.on(events.NewMessage(pattern=r"^/setcaption\s+(.+)$"))
    @admin_only
    async def setcaption_cmd(event: events.NewMessage.Event) -> None:
        await _set_channel_or_global(ctx, bot, event, "caption", event.pattern_match.group(1))

    @bot.on(events.NewMessage(pattern=r"^/setfooter\s+(.+)$"))
    @admin_only
    async def setfooter_cmd(event: events.NewMessage.Event) -> None:
        await _set_channel_or_global(ctx, bot, event, "footer", event.pattern_match.group(1))

    @bot.on(events.NewMessage(pattern=r"^/settings$"))
    @admin_only
    async def settings_cmd(event: events.NewMessage.Event) -> None:
        from uploader.handlers import menu as menu_mod

        text, buttons = await menu_mod.render_settings(ctx)
        await event.reply(text, buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"^/proxy$"))
    @admin_only
    async def proxy_cmd(event: events.NewMessage.Event) -> None:
        proxy = ctx.settings.tg_proxy_config
        if proxy is None:
            await event.reply("پراکسی تنظیم نشده است.")
            return
        start = time.monotonic()
        ok, detail = await _test_proxy(ctx)
        elapsed = (time.monotonic() - start) * 1000
        status = "✅ متصل" if ok else f"⛔️ خطا: {detail}"
        await event.reply(
            f"پراکسی: {proxy.scheme}://{proxy.host}:{proxy.port}\n"
            f"وضعیت: {status}\n"
            f"تأخیر: {elapsed:.0f}ms"
        )


async def _resolve_channel_id(ctx: AppContext, bot, target: str):
    channels = await ctx.db.list_channels()
    for c in channels:
        if target in (str(c.chat_id), f"@{c.username}", c.username):
            return c.chat_id
    try:
        from telethon.utils import get_peer_id

        entity = await bot.get_entity(target)
        return get_peer_id(entity)
    except Exception:  # noqa: BLE001
        return None


async def _set_channel_or_global(ctx: AppContext, bot, event, kind: str, raw: str) -> None:
    parts = raw.split(maxsplit=1)
    chat_id = None
    value = raw
    if len(parts) == 2:
        candidate_id = await _resolve_channel_id(ctx, bot, parts[0])
        if candidate_id is not None:
            chat_id = candidate_id
            value = parts[1]

    if chat_id is not None:
        if kind == "caption":
            await ctx.db.set_channel_caption(chat_id, value)
        else:
            await ctx.db.set_channel_footer(chat_id, value)
        await event.reply("✅ برای کانال انتخابی ذخیره شد.")
    else:
        key = "default_caption_template" if kind == "caption" else "default_footer"
        await ctx.db.set_setting(key, value)
        await event.reply("✅ به صورت سراسری ذخیره شد.")


async def render_settings_menu(ctx: AppContext):
    settings = await ctx.db.all_settings()
    rows = []
    for key, label in (
        ("force_document", "ارسال به صورت فایل (Document)"),
        ("auto_thumbnail", "تولید خودکار تامبنیل"),
        ("split_mode", "تقسیم خودکار فایل‌های حجیم"),
    ):
        mark = "✅" if settings.get(key) == "1" else "⬜️"
        rows.append([Button.inline(f"{mark} {label}", data=f"toggle:{key}".encode())])
    text = (
        "⚙️ تنظیمات\n"
        f"هم‌زمانی دانلود: {settings.get('download_concurrency')}\n"
        f"هم‌زمانی آپلود: {settings.get('upload_concurrency')}\n"
        "(برای تغییر هم‌زمانی، متغیرهای DOWNLOAD_CONCURRENCY/UPLOAD_CONCURRENCY را در .env ویرایش و ربات را ریستارت کنید)"
    )
    return text, rows


async def _test_proxy(ctx: AppContext):
    proxy = ctx.settings.tg_proxy_config
    if proxy is None:
        return False, "no proxy configured"
    try:
        import aiohttp
        from aiohttp_socks import ProxyConnector

        connector = ProxyConnector.from_url(proxy.as_aiohttp_socks_url())
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get("https://api.telegram.org") as resp:
                return resp.status < 500, f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
