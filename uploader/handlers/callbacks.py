"""Inline-button callback handlers: job controls, channel picking, settings toggles."""

from __future__ import annotations

import logging
import secrets

from telethon import events

from uploader.context import AppContext

logger = logging.getLogger(__name__)

TOGGLE_LABELS = {
    "force_document": "ارسال به صورت فایل (Document)",
    "auto_thumbnail": "تولید خودکار تامبنیل",
    "split_mode": "تقسیم خودکار فایل‌های حجیم",
}


def register(ctx: AppContext) -> None:
    @ctx.bot.on(events.CallbackQuery())
    async def on_callback(event: events.CallbackQuery.Event) -> None:
        if not ctx.is_admin(event.sender_id):
            return
        data = event.data.decode("utf-8", errors="ignore")
        action, _, rest = data.partition(":")

        if action == "cancel":
            job_id = int(rest)
            ok = ctx.job_manager.cancel_job(job_id)
            await event.answer("در حال لغو..." if ok else "این کار دیگر فعال نیست.")
            await _refresh_queue_if_applicable(ctx, event)
        elif action == "pause":
            job_id = int(rest)
            new_state = ctx.job_manager.toggle_pause(job_id)
            if new_state is None:
                await event.answer("این کار در وضعیت دانلود نیست.")
            else:
                await event.answer("⏸ متوقف شد" if new_state else "▶ ادامه یافت")
            await _refresh_queue_if_applicable(ctx, event)
        elif action == "chpick":
            await _handle_channel_pick(ctx, event, rest)
        elif action == "chconfirm":
            await _handle_channel_confirm(ctx, event, rest)
        elif action == "toggle":
            await _handle_settings_toggle(ctx, event, rest)
        elif action == "retry":
            job_id = int(rest)
            ok = await ctx.job_manager.retry(job_id)
            await event.answer("دوباره به صف اضافه شد" if ok else "این کار قابل تلاش دوباره نیست.")
            await _refresh_queue_if_applicable(ctx, event)
        elif action == "menu":
            from uploader.handlers import menu as menu_mod

            text, buttons = await menu_mod.render_menu(ctx, rest)
            await event.edit(text, buttons=buttons)
            await event.answer()
        elif action == "chan":
            await _handle_channel_action(ctx, event, rest)
        elif action == "scrape":
            await _handle_scrape_pick(ctx, event, rest)
        else:
            await event.answer()


async def _refresh_queue_if_applicable(ctx: AppContext, event: events.CallbackQuery.Event) -> None:
    """Best-effort: these buttons only ever appear inside the queue menu, so refresh it in place."""
    from uploader.handlers import menu as menu_mod

    try:
        text, buttons = await menu_mod.render_queue(ctx)
        await event.edit(text, buttons=buttons)
    except Exception:  # noqa: BLE001 - never let a UI refresh crash the callback
        logger.debug("Could not refresh queue menu after job action", exc_info=True)


async def _handle_channel_pick(ctx: AppContext, event: events.CallbackQuery.Event, rest: str) -> None:
    from uploader.handlers.links import _channel_keyboard

    token, _, chat_id_str = rest.partition(":")
    batch = ctx.pending_batches.get(token)
    if batch is None:
        await event.answer("این انتخاب منقضی شده است.")
        return
    chat_id = int(chat_id_str)
    if chat_id in batch["selected"]:
        if len(batch["selected"]) > 1:
            batch["selected"].discard(chat_id)
    else:
        batch["selected"].add(chat_id)
    await event.edit(buttons=_channel_keyboard(ctx, token))
    await event.answer()


async def _handle_channel_confirm(ctx: AppContext, event: events.CallbackQuery.Event, token: str) -> None:
    batch = ctx.pending_batches.pop(token, None)
    if batch is None:
        await event.answer("این انتخاب منقضی شده است.")
        return
    channel_ids = list(batch["selected"])
    urls = batch["urls"]
    batch_id = secrets.token_hex(8) if len(urls) > 1 else None
    for index, url in enumerate(urls):
        await ctx.job_manager.enqueue(
            batch["admin_chat_id"], url, batch["caption"], channel_ids, batch_id=batch_id, batch_index=index
        )
    note = " (پس از دانلود کامل همه، در یک آلبوم ارسال می‌شوند)" if len(urls) > 1 else ""
    await event.edit(f"✅ {len(urls)} لینک به صف اضافه شد.{note}", buttons=None)
    await event.answer()


async def _handle_settings_toggle(ctx: AppContext, event: events.CallbackQuery.Event, key: str) -> None:
    from uploader.handlers import menu as menu_mod

    current = (await ctx.db.get_setting(key)) == "1"
    await ctx.db.set_setting(key, "0" if current else "1")
    text, buttons = await menu_mod.render_settings(ctx)
    await event.edit(text, buttons=buttons)
    await event.answer("ذخیره شد")


async def _handle_channel_action(ctx: AppContext, event: events.CallbackQuery.Event, rest: str) -> None:
    from uploader.handlers import menu as menu_mod

    parts = rest.split(":")
    sub = parts[0]

    if sub == "add":
        ctx.pending_inputs[event.sender_id] = {"action": "addchannel"}
        await event.answer()
        await event.respond(
            "آیدی عددی یا یوزرنیم کانال (@username) را بفرستید،\n"
            "یا یک پیام از آن کانال را برای من فوروارد کنید."
        )
        return

    chat_id = int(parts[1])

    if sub == "detail":
        text, buttons = await menu_mod.render_channel_detail(ctx, chat_id)
        await event.edit(text, buttons=buttons)
        await event.answer()
    elif sub == "setdefault":
        await ctx.db.set_default_channel(chat_id)
        text, buttons = await menu_mod.render_channel_detail(ctx, chat_id)
        await event.edit(text, buttons=buttons)
        await event.answer("تنظیم شد")
    elif sub == "delete":
        await ctx.db.remove_channel(chat_id)
        text, buttons = await menu_mod.render_channels(ctx)
        await event.edit(text, buttons=buttons)
        await event.answer("حذف شد")
    elif sub == "setcaption":
        ctx.pending_inputs[event.sender_id] = {"action": "setcaption", "chat_id": chat_id}
        await event.answer()
        await event.respond(
            "قالب کپشن جدید را بفرستید.\nمتغیرها: {filename} {size} {duration} {resolution} {ext} {date} {index}"
        )
    elif sub == "setfooter":
        ctx.pending_inputs[event.sender_id] = {"action": "setfooter", "chat_id": chat_id}
        await event.answer()
        await event.respond("متن فوتر جدید را بفرستید.")
    else:
        await event.answer()


async def _handle_scrape_pick(ctx: AppContext, event: events.CallbackQuery.Event, rest: str) -> None:
    token, _, index_str = rest.partition(":")
    data = ctx.pending_scrapes.get(token)
    if data is None:
        await event.answer("این منو منقضی شده است.")
        return
    try:
        item = data["items"][int(index_str)]
    except (IndexError, ValueError):
        await event.answer("این مورد دیگر در دسترس نیست.")
        return

    channels = await ctx.db.list_channels()
    if not channels:
        await event.answer("هیچ کانالی تنظیم نشده است.")
        return

    if len(channels) == 1:
        channel_ids = [channels[0].chat_id]
        await ctx.job_manager.enqueue(data["admin_chat_id"], item.url, data["caption"], channel_ids)
        await event.answer(f"✅ «{item.label}» به صف اضافه شد.")
        return

    from uploader.handlers.links import _channel_keyboard

    default = next((c for c in channels if c.is_default), channels[0])
    pick_token = secrets.token_hex(8)
    ctx.pending_batches[pick_token] = {
        "urls": [item.url],
        "caption": data["caption"],
        "admin_chat_id": data["admin_chat_id"],
        "channels": channels,
        "selected": {default.chat_id},
    }
    await event.respond(
        f"کانال مقصد برای «{item.label}» را انتخاب کنید:",
        buttons=_channel_keyboard(ctx, pick_token),
    )
    await event.answer()
