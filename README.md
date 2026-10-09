# Telegram Uploader Bot (Telethon / MTProto)

A production-grade admin-only uploader bot. Send it a direct download link and it
downloads the file and re-uploads it to one or more private Telegram channels with
a formatted caption — fully async, resumable, proxy-aware, and queue-persistent.

Not built on the Bot API (python-telegram-bot). Uses raw MTProto via
[Telethon](https://docs.telethon.dev/), plus a parallel-connection ("FastTelethon"-style)
uploader for large files.

## Features

- Bot client (BOT_TOKEN) for admin chat + optional user client (phone-number session)
  for uploads up to 2 GB (4 GB on Telegram Premium).
- SOCKS5/SOCKS4/HTTP proxy support for both the Telegram connection and downloads.
- Resumable, segmented (multi-connection) downloads with retry/backoff.
- Parallel multi-connection uploads with automatic fallback to Telethon's normal uploader.
- Video/audio/image-aware sending: ffprobe metadata, auto thumbnails, streaming flag.
- Automatic splitting (7z volumes, or ffmpeg segments for video) + album upload for
  files over Telegram's size limit.
- Live progress message with a bar, speed, ETA, and Cancel / Pause-Resume buttons.
- Persistent SQLite job queue that survives restarts, with configurable concurrency.
- Caption templating ({filename}, {size}, {duration}, {resolution}, {ext},
  {date}, {index}), per-channel defaults + footer.
- Multi-channel management, URL/content de-duplication, automatic cleanup.
- Admin commands: `/queue`, `/cancel`, `/retry`, `/history`, `/stats`, `/channels`,
  `/addchannel`, `/delchannel`, `/setdefault`, `/setcaption`, `/setfooter`,
  `/settings`, `/proxy`.

## Project layout

```
uploader/
  main.py            entrypoint, client setup, graceful shutdown
  config.py          pydantic settings
  context.py         the one shared mutable app context
  db.py              aiosqlite models/queries
  queue.py           job manager and workers
  downloader.py      resumable/segmented downloader
  uploader.py        fast parallel upload + media attributes + splitting
  media.py           ffprobe/ffmpeg helpers
  progress.py        throttled progress tracking + rendering
  utils.py           filename/caption/hash helpers
  handlers/          command, message and callback handlers
login.py             interactive StringSession generator
tests/               unit tests (filename parsing, captions, progress)
```

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` on PATH (video/audio metadata + thumbnails)
- `7z` (p7zip) on PATH (splitting non-video files that exceed the size limit)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

1. `API_ID` / `API_HASH` from <https://my.telegram.org>.
2. `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).
3. `ADMIN_IDS` — your Telegram numeric user id(s), comma separated.
4. `TG_PROXY` — only if you need a SOCKS5/SOCKS4/HTTP proxy to reach Telegram.
5. (Optional) generate a user session for bigger uploads:

   ```bash
   python login.py
   ```

   Paste the printed string into `USER_SESSION`.

Run it:

```bash
python -m uploader.main
```

Then, in a private chat with the bot, add a channel the bot/user account is an
admin of:

```
/addchannel @my_channel
```

and send a link:

```
https://example.com/file.mkv
My custom caption line
```

Multiple links with a shared caption:

```
https://example.com/a.mkv
https://example.com/b.mkv
---
Shared caption for both files
```

## Tests

```bash
pytest
```

## Docker

```bash
cp .env.example .env   # fill it in first
docker compose up -d --build
```

## systemd (bare-metal)

```bash
sudo useradd -r -m -d /opt/uploader uploader
sudo cp -r . /opt/uploader
cd /opt/uploader
sudo -u uploader python3 -m venv .venv
sudo -u uploader .venv/bin/pip install -r requirements.txt
sudo cp uploader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now uploader
```

---

## راهنمای فارسی

این ربات یک آپلودر خصوصی برای ادمین‌هاست که با پروتکل MTProto (کتابخانه Telethon)
کار می‌کند — نه Bot API. لینک دانلود مستقیم را برای ربات بفرستید تا فایل را دانلود
و با کپشن دلخواه در یک یا چند کانال خصوصی تلگرام آپلود کند.

### نصب سریع

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

مقادیر زیر را در `.env` تنظیم کنید:

- `API_ID` و `API_HASH` از my.telegram.org
- `BOT_TOKEN` از @BotFather
- `ADMIN_IDS` — شناسه عددی کاربری شما در تلگرام
- `TG_PROXY` — در صورت نیاز به پراکسی SOCKS5/SOCKS4/HTTP
- در صورت نیاز به آپلود فایل‌های بزرگ‌تر (تا ۲ یا ۴ گیگابایت)، با دستور
  `python login.py` یک نشست کاربری بسازید و در `USER_SESSION` قرار دهید.

### اجرا

```bash
python -m uploader.main
```

در چت خصوصی با ربات، ابتدا یک کانال اضافه کنید:

```
/addchannel @my_channel
```

سپس لینک دانلود را بفرستید (و در صورت تمایل، کپشن را در خط‌های بعدی اضافه کنید).
برای چند لینک با یک کپشن مشترک، لینک‌ها را هرکدام در یک خط و بعد از یک خط `---`
کپشن مشترک را بنویسید.

### دستورات مهم

`/queue`، `/cancel <id>`، `/retry <id>`، `/history`، `/stats`، `/channels`،
`/addchannel`، `/delchannel`، `/setdefault`، `/setcaption`، `/setfooter`،
`/settings`، `/proxy`.
