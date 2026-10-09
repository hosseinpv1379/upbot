"""Entrypoint: builds both Telethon clients, wires handlers, and runs until shutdown."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from uploader.config import Settings, load_settings
from uploader.context import DEFAULT_UPLOAD_LIMIT, PREMIUM_UPLOAD_LIMIT, AppContext
from uploader.db import Database
from uploader.handlers import register_all
from uploader.queue import JobManager

logger = logging.getLogger("uploader")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "uploader.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("telethon").setLevel(logging.WARNING)


async def check_proxy(settings: Settings) -> None:
    proxy = settings.tg_proxy_config
    if proxy is None:
        return
    import python_socks
    from python_socks.async_.asyncio import Proxy

    try:
        client = Proxy.create(
            proxy_type=proxy.as_telethon_tuple()[0],
            host=proxy.host,
            port=proxy.port,
            username=proxy.username,
            password=proxy.password,
            rdns=proxy.rdns,
        )
        sock = await asyncio.wait_for(client.connect(dest_host="149.154.167.50", dest_port=443), timeout=10)
        sock.close()
        logger.info("Proxy %s:%s is reachable", proxy.host, proxy.port)
    except python_socks.ProxyError as exc:
        logger.error("Proxy authentication/negotiation failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Proxy %s:%s is NOT reachable: %s", proxy.host, proxy.port, exc)


def build_client(session, settings: Settings) -> TelegramClient:
    proxy = settings.tg_proxy_config
    kwargs = {}
    if proxy is not None:
        kwargs["proxy"] = proxy.as_telethon_tuple()
    client = TelegramClient(session, settings.api_id, settings.api_hash, **kwargs)
    return client


async def determine_upload_limit(user_client: TelegramClient | None) -> int:
    if user_client is None:
        return DEFAULT_UPLOAD_LIMIT
    try:
        me = await user_client.get_me()
        if getattr(me, "premium", False):
            return PREMIUM_UPLOAD_LIMIT
    except Exception:  # noqa: BLE001
        logger.exception("Could not determine Premium status, assuming the 2 GB limit")
    return DEFAULT_UPLOAD_LIMIT


async def run() -> None:
    settings = load_settings()
    setup_logging(settings.log_dir)
    logger.info("Starting uploader bot")

    await check_proxy(settings)

    db = Database(settings.db_path)
    await db.connect()

    bot = build_client("bot_session", settings)
    await bot.start(bot_token=settings.bot_token)
    logger.info("Bot client connected as @%s", (await bot.get_me()).username)

    user_client = None
    if settings.user_session:
        user_client = build_client(StringSession(settings.user_session), settings)
        await user_client.connect()
        if not await user_client.is_user_authorized():
            logger.error("USER_SESSION is set but not authorized; run login.py to regenerate it. Falling back to bot client.")
            await user_client.disconnect()
            user_client = None
        else:
            me = await user_client.get_me()
            logger.info("User client connected as %s", me.username or me.id)

    upload_limit = await determine_upload_limit(user_client)
    logger.info("Effective upload limit: %s bytes", upload_limit)

    ctx = AppContext(settings=settings, db=db, bot=bot, user_client=user_client, upload_limit_bytes=upload_limit)
    ctx.job_manager = JobManager(ctx)

    register_all(ctx)
    await ctx.job_manager.start()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, AttributeError):
            # Windows has no event-loop signal handlers; fall back to the classic API.
            signal.signal(sig, lambda *_: _signal_handler())

    logger.info("Uploader bot is ready")
    await stop_event.wait()

    logger.info("Shutting down, waiting for in-flight jobs to checkpoint...")
    await ctx.job_manager.shutdown()
    await bot.disconnect()
    if user_client is not None:
        await user_client.disconnect()
    await db.close()
    logger.info("Shutdown complete")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
