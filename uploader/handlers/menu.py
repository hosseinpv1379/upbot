"""Inline-button ("glass button") menu rendering — the primary admin UI.

Pure render functions: each returns ``(text, buttons)`` for the caller (a command
handler or a callback handler) to send or edit into a message. No Telethon event
registration happens here; that lives in ``commands.py`` (entry points) and
``callbacks.py`` (navigation/actions), both of which import from this module.
"""

from __future__ import annotations

from telethon import Button

from uploader.context import AppContext
from uploader.utils import free_disk_space, human_duration, human_size

MAIN_MENU_TEXT = "🤖 منوی ربات آپلودر\n\nیک گزینه را انتخاب کنید، یا یک لینک دانلود را مستقیم بفرستید."

STATUS_FA = {
    "queued": "⏳ در صف",
    "downloading": "⬇️ دانلود",
    "uploading": "⬆️ آپلود",
    "paused": "⏸ متوقف",
    "done": "✅ تمام‌شده",
    "failed": "⛔️ ناموفق",
    "cancelled": "❌ لغوشده",
}


def main_menu_buttons() -> list:
    return [
        [Button.inline("📋 صف کارها", data=b"menu:queue"), Button.inline("📡 کانال‌ها", data=b"menu:channels")],
        [Button.inline("🕘 تاریخچه", data=b"menu:history"), Button.inline("📊 آمار", data=b"menu:stats")],
        [Button.inline("⚙️ تنظیمات", data=b"menu:settings"), Button.inline("🌐 پراکسی", data=b"menu:proxy")],
        [Button.inline("❓ راهنما", data=b"menu:help")],
    ]


def back_button(target: str = "menu:main") -> list:
    return [[Button.inline("🔙 بازگشت", data=target.encode())]]


async def render_main(ctx: AppContext) -> tuple[str, list]:
    return MAIN_MENU_TEXT, main_menu_buttons()


async def render_queue(ctx: AppContext) -> tuple[str, list]:
    active_jobs = await ctx.db.list_jobs(statuses=("queued", "downloading", "uploading", "paused"), limit=15)
    failed_jobs = await ctx.db.list_jobs(statuses=("failed", "cancelled"), limit=5)

    if not active_jobs and not failed_jobs:
        return "صف خالی است.", [[Button.inline("🔄 بروزرسانی", data=b"menu:queue")]] + back_button()

    def short(label: str) -> str:
        return label if len(label) <= 40 else label[:37] + "..."

    lines = []
    rows = []
    if active_jobs:
        lines.append("📋 صف کارها:")
        for job in active_jobs:
            label = short(job.filename or job.url)
            lines.append(f"#{job.id} — {label} — {STATUS_FA.get(job.status, job.status)}")
            rows.append(
                [
                    Button.inline(f"❌ #{job.id}", data=f"cancel:{job.id}".encode()),
                    Button.inline(f"⏸/▶ #{job.id}", data=f"pause:{job.id}".encode()),
                ]
            )
    if failed_jobs:
        lines.append("\n⛔️ ناموفق/لغوشده‌های اخیر:")
        for job in failed_jobs:
            label = short(job.filename or job.url)
            lines.append(f"#{job.id} — {label} — {STATUS_FA.get(job.status, job.status)}")
            rows.append([Button.inline(f"🔁 تلاش دوباره #{job.id}", data=f"retry:{job.id}".encode())])

    rows.append([Button.inline("🔄 بروزرسانی", data=b"menu:queue")])
    rows.extend(back_button())
    return "\n".join(lines), rows


async def render_channels(ctx: AppContext) -> tuple[str, list]:
    channels = await ctx.db.list_channels()
    rows = []
    for c in channels:
        mark = "⭐️" if c.is_default else "▫️"
        label = f"{mark} {c.title or c.username or c.chat_id}"
        rows.append([Button.inline(label, data=f"chan:detail:{c.chat_id}".encode())])
    rows.append([Button.inline("➕ افزودن کانال", data=b"chan:add")])
    rows.extend(back_button())
    text = "📡 کانال‌های تنظیم‌شده:" if channels else "هنوز کانالی تنظیم نشده است. با «➕ افزودن کانال» شروع کنید."
    return text, rows


async def render_channel_detail(ctx: AppContext, chat_id: int) -> tuple[str, list]:
    channels = await ctx.db.list_channels()
    channel = next((c for c in channels if c.chat_id == chat_id), None)
    if channel is None:
        return "این کانال دیگر وجود ندارد.", back_button("menu:channels")

    lines = [f"📡 {channel.title or channel.username or channel.chat_id}", f"آیدی: {channel.chat_id}"]
    if channel.username:
        lines.append(f"یوزرنیم: @{channel.username}")
    lines.append("⭐️ کانال پیش‌فرض" if channel.is_default else "▫️ پیش‌فرض نیست")
    lines.append(f"کپشن: {channel.caption_template or '(سراسری)'}")
    lines.append(f"فوتر: {channel.footer or '(سراسری)'}")

    rows = []
    if not channel.is_default:
        rows.append([Button.inline("⭐️ تعیین به عنوان پیش‌فرض", data=f"chan:setdefault:{chat_id}".encode())])
    rows.append(
        [
            Button.inline("✏️ تغییر کپشن", data=f"chan:setcaption:{chat_id}".encode()),
            Button.inline("📝 تغییر فوتر", data=f"chan:setfooter:{chat_id}".encode()),
        ]
    )
    rows.append([Button.inline("🗑 حذف کانال", data=f"chan:delete:{chat_id}".encode())])
    rows.extend(back_button("menu:channels"))
    return "\n".join(lines), rows


async def render_history(ctx: AppContext) -> tuple[str, list]:
    from uploader.queue import build_message_link

    rows_db = await ctx.db.list_history(limit=10)
    if not rows_db:
        return "هنوز آپلودی ثبت نشده است.", back_button()

    channels = await ctx.db.list_channels()
    lines = ["🕘 آخرین آپلودها:"]
    for row in rows_db:
        channel = next((c for c in channels if c.chat_id == row["channel_id"]), None)
        link = build_message_link(row["channel_id"], row["message_id"], channel.username if channel else None)
        lines.append(f"\n{row['filename']} ({human_size(row['file_size'])})\n{link}")
    return "\n".join(lines), back_button()


async def render_stats(ctx: AppContext) -> tuple[str, list]:
    stats = await ctx.db.stats()
    free = free_disk_space(ctx.settings.download_dir)
    proxy = ctx.settings.tg_proxy_config
    proxy_text = f"{proxy.scheme}://{proxy.host}:{proxy.port}" if proxy else "غیرفعال"
    by_status = ", ".join(f"{k}: {v}" for k, v in stats["jobs_by_status"].items()) or "—"
    text = (
        "📊 آمار ربات\n"
        f"تعداد آپلودها: {stats['uploads']}\n"
        f"حجم کل: {human_size(stats['total_bytes'])}\n"
        f"وضعیت کارها: {by_status}\n"
        f"فضای آزاد دیسک: {human_size(free)}\n"
        f"زمان روشن بودن: {human_duration(ctx.uptime_seconds)}\n"
        f"پراکسی: {proxy_text}"
    )
    return text, back_button()


async def render_proxy(ctx: AppContext) -> tuple[str, list]:
    proxy = ctx.settings.tg_proxy_config
    if proxy is None:
        return "پراکسی تنظیم نشده است.", back_button()

    from uploader.handlers.commands import _test_proxy

    ok, detail = await _test_proxy(ctx)
    status = "✅ متصل" if ok else f"⛔️ خطا: {detail}"
    text = f"پراکسی: {proxy.scheme}://{proxy.host}:{proxy.port}\nوضعیت: {status}"
    rows = [[Button.inline("🔄 تست مجدد", data=b"menu:proxy")]]
    rows.extend(back_button())
    return text, rows


async def render_settings(ctx: AppContext) -> tuple[str, list]:
    from uploader.handlers.commands import render_settings_menu

    text, rows = await render_settings_menu(ctx)
    return text, rows + back_button()


async def render_help(ctx: AppContext) -> tuple[str, list]:
    from uploader.handlers.commands import HELP_TEXT

    return HELP_TEXT, back_button()


RENDERERS = {
    "main": render_main,
    "queue": render_queue,
    "channels": render_channels,
    "history": render_history,
    "stats": render_stats,
    "proxy": render_proxy,
    "settings": render_settings,
    "help": render_help,
}


async def render_menu(ctx: AppContext, key: str) -> tuple[str, list]:
    renderer = RENDERERS.get(key, render_main)
    return await renderer(ctx)
