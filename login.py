#!/usr/bin/env python3
"""Interactive StringSession generator for the optional user (uploader) client.

Run this once to log in with a phone number (supports 2FA) and print a
StringSession to paste into USER_SESSION in your .env file. This session is
only used to upload files with a higher size limit (2 GB, or 4 GB on
Telegram Premium) — the bot itself always authenticates with BOT_TOKEN.
"""

from __future__ import annotations

import asyncio
import getpass
import os

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

try:
    from dotenv import dotenv_values

    _ENV = dotenv_values(".env")
except ImportError:  # pragma: no cover
    _ENV = {}


def _env(key: str, prompt: str) -> str:
    value = os.environ.get(key) or _ENV.get(key)
    if value:
        return value
    return input(prompt)


async def main() -> None:
    api_id = int(_env("API_ID", "API_ID: "))
    api_hash = _env("API_HASH", "API_HASH: ")
    proxy = None
    proxy_url = os.environ.get("TG_PROXY") or _ENV.get("TG_PROXY")
    if proxy_url:
        from uploader.config import ProxyConfig

        proxy = ProxyConfig(proxy_url).as_telethon_tuple()

    client = TelegramClient(StringSession(), api_id, api_hash, proxy=proxy)
    await client.connect()

    phone = input("شماره تلفن (با کد کشور، مثل +98...): ").strip()
    sent = await client.send_code_request(phone)
    code = input("کد ارسال‌شده به تلگرام را وارد کنید: ").strip()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        password = getpass.getpass("رمز دو مرحله‌ای (2FA): ")
        await client.sign_in(password=password)

    me = await client.get_me()
    session_string = client.session.save()

    print("\n✅ ورود موفق:", me.first_name, me.username or "")
    print("\nاین مقدار را در فایل .env به عنوان USER_SESSION قرار دهید:\n")
    print(session_string)
    print()

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
