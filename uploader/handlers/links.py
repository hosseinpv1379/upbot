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

        forward = event.message.forward
        if forward is not None and forward.is_channel:
            await _handle_forwarded_channel(ctx, event, forward)
            ctx.pending_inputs.pop(sender_id, None)
            return

        pending = ctx.pending_inputs.pop(sender_id, None)
        if pending is not None:
            await _handle_pending_input(ctx, event, pending)
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


async def _handle_forwarded_channel(ctx: AppContext, event: events.NewMessage.Event, forward) -> None:
    """Registers the source channel of a forwarded post — the reliable way to resolve a
    channel Telethon has never interacted with, since a bare numeric chat id can't be
    resolved without an access hash, but a forwarded post carries one."""
    from telethon.utils import get_peer_id

    try:
        chat = await forward.get_chat()
    except Exception:  # noqa: BLE001
        chat = None

    if chat is None:
        await event.reply(
            "چنل منبع این پیام فوروارد‌شده قابل شناسایی نبود.\n"
            "اگر بات/اکانت در آن کانال ادمین نیست، ابتدا ادمینش کنید و دوباره پیام را فوروارد کنید."
        )
        return

    chat_id = get_peer_id(chat)
    title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)
    username = getattr(chat, "username", None)
    await ctx.db.add_channel(chat_id, title, username)
    await event.reply(f"✅ کانال «{title}» ({chat_id}) از روی پیام فوروارد‌شده اضافه شد.")


async def _handle_pending_input(ctx: AppContext, event: events.NewMessage.Event, pending: dict) -> None:
    """Consumes a plain-text reply the admin sent after tapping an inline-menu action
    that needs free text (add channel by id/username, new caption, new footer)."""
    from uploader.handlers import menu as menu_mod

    action = pending["action"]
    value = (event.raw_text or "").strip()
    if not value:
        await event.reply("متن خالی بود، دوباره تلاش کنید.")
        return

    if action == "addchannel":
        try:
            entity = await ctx.bot.get_entity(value)
        except Exception as exc:  # noqa: BLE001
            await event.reply(
                f"کانال پیدا نشد: {exc}\nمی‌توانید به‌جای آن، یک پیام از آن کانال را فوروارد کنید."
            )
            return
        from telethon.utils import get_peer_id

        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)
        username = getattr(entity, "username", None)
        chat_id = get_peer_id(entity)
        await ctx.db.add_channel(chat_id, title, username)
        text, buttons = await menu_mod.render_channels(ctx)
        await event.reply(f"✅ کانال «{title}» اضافه شد.", buttons=buttons)
    elif action == "setcaption":
        await ctx.db.set_channel_caption(pending["chat_id"], value)
        text, buttons = await menu_mod.render_channel_detail(ctx, pending["chat_id"])
        await event.reply(text, buttons=buttons)
    elif action == "setfooter":
        await ctx.db.set_channel_footer(pending["chat_id"], value)
        text, buttons = await menu_mod.render_channel_detail(ctx, pending["chat_id"])
        await event.reply(text, buttons=buttons)
