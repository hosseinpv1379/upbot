"""Telethon event handler registration."""

from __future__ import annotations

from uploader.context import AppContext
from uploader.handlers import callbacks, commands, links


def register_all(ctx: AppContext) -> None:
    commands.register(ctx)
    links.register(ctx)
    callbacks.register(ctx)
