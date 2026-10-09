"""Inline-button callback handlers: job controls, channel picking, settings toggles."""

from __future__ import annotations

import logging

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
        elif action == "pause":
            job_id = int(rest)
            new_state = ctx.job_manager.toggle_pause(job_id)
            if new_state is None:
                await event.answer("این کار در وضعیت دانلود نیست.")
            else:
                await event.answer("⏸ متوقف شد" if new_state else "▶ ادامه یافت")
        elif action == "chpick":
            await _handle_channel_pick(ctx, event, rest)
        elif action == "chconfirm":
            await _handle_channel_confirm(ctx, event, rest)
        elif action == "toggle":
            await _handle_settings_toggle(ctx, event, rest)
        else:
            await event.answer()


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
    for url in batch["urls"]:
        await ctx.job_manager.enqueue(batch["admin_chat_id"], url, batch["caption"], channel_ids)
    await event.edit(f"✅ {len(batch['urls'])} لینک به صف اضافه شد.", buttons=None)
    await event.answer()


async def _handle_settings_toggle(ctx: AppContext, event: events.CallbackQuery.Event, key: str) -> None:
    from uploader.handlers.commands import render_settings_menu

    current = (await ctx.db.get_setting(key)) == "1"
    await ctx.db.set_setting(key, "0" if current else "1")
    text, buttons = await render_settings_menu(ctx)
    await event.edit(text, buttons=buttons)
    await event.answer("ذخیره شد")
