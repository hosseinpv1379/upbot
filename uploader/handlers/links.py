"""Handles plain messages from admins: one or more download links plus an optional caption."""

from __future__ import annotations

import logging
import secrets

from telethon import Button, events

from uploader.context import AppContext
from uploader.utils import parse_links_message

logger = logging.getLogger(__name__)


def _channel_keyboard(ctx: AppContext, token: str) -> list:
    batch = ctx.pending_batches[token]
    selected = batch["selected"]
    rows = []
    for channel in batch["channels"]:
        mark = "✅" if channel.chat_id in selected else "⬜️"
        label = f"{mark} {channel.title or channel.username or channel.chat_id}"
        rows.append([Button.inline(label, data=f"chpick:{token}:{channel.chat_id}".encode())])
    rows.append([Button.inline("🚀 شروع آپلود", data=f"chconfirm:{token}".encode())])
    return rows


def register(ctx: AppContext) -> None:
    @ctx.bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_message(event: events.NewMessage.Event) -> None:
        sender_id = event.sender_id
        if not ctx.is_admin(sender_id):
            return
        text = event.raw_text or ""
        if text.startswith("/"):
            return  # commands are handled separately

        urls, caption = parse_links_message(text)
        if not urls:
            await event.reply("لینک معتبری پیدا نشد. هر لینک باید با http:// یا https:// شروع شود.")
            return

        channels = await ctx.db.list_channels()
        if not channels:
            await event.reply("هیچ کانالی تنظیم نشده است. ابتدا با /addchannel یک کانال اضافه کنید.")
            return

        if len(channels) == 1:
            channel_ids = [channels[0].chat_id]
            await _queue_batch(ctx, event.chat_id, urls, caption, channel_ids)
            await event.reply(f"✅ {len(urls)} لینک به صف اضافه شد.")
            return

        default = next((c for c in channels if c.is_default), channels[0])
        token = secrets.token_hex(8)
        ctx.pending_batches[token] = {
            "urls": urls,
            "caption": caption,
            "admin_chat_id": event.chat_id,
            "channels": channels,
            "selected": {default.chat_id},
        }
        await event.reply(
            f"کانال مقصد برای {len(urls)} لینک را انتخاب کنید:",
            buttons=_channel_keyboard(ctx, token),
        )


async def _queue_batch(ctx: AppContext, admin_chat_id: int, urls: list[str], caption: str, channel_ids: list[int]) -> None:
    for url in urls:
        await ctx.job_manager.enqueue(admin_chat_id, url, caption, channel_ids)
