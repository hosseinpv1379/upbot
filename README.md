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
- Multiple links sent together (one per line, no `---` needed) are downloaded in
  parallel and then posted as a single Telegram album (media group) with one
  shared caption, in the original link order, once every file in the batch has
  finished downloading.
- Page scraping: send a single link that isn't itself a media file (e.g. a song's
  page), and the bot fetches it, finds every audio/video link on the page
  (`<a href>`, `<audio>`/`<video>`/`<source>`, `og:audio`/`og:video`), and shows
  them as inline buttons — tap one to download + upload just that file. Only
  catches links whose URL has a recognizable media extension; pages that stream
  through extension-less endpoints won't be picked up.
- Live progress message with a bar, speed, ETA, and Cancel / Pause-Resume buttons.
- Persistent SQLite job queue that survives restarts, with configurable concurrency.
- Caption templating ({filename}, {size}, {duration}, {resolution}, {ext},
  {date}, {index}), per-channel defaults + footer.
- Multi-channel management, URL/content de-duplication, automatic cleanup.
- Full inline-button ("glass button") menu — `/menu` — for everything below, so
  admins never have to type a command: queue (with cancel/pause/retry buttons),
  channel management (add via forwarded post or id/username, set default, edit
  caption/footer, delete), history, stats, settings toggles, proxy test.
- The same actions are also available as admin commands, if you prefer typing:
  `/queue`, `/cancel`, `/retry`, `/history`, `/stats`, `/channels`,
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
  scraper.py         HTML page scraping for audio/video links
  progress.py        throttled progress tracking + rendering
  utils.py           filename/caption/hash helpers
  handlers/
    menu.py          inline-button menu rendering (the /menu UI)
    commands.py      admin slash-commands
    links.py         download links + pending text replies for the menu
    callbacks.py     inline-button callback handling
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

Multiple links, with or without a shared caption — once every file has finished
downloading, they're posted together as a single Telegram album:

```
https://example.com/a.mkv
https://example.com/b.mkv
```

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
اگر چند لینک را با هم بفرستید (هرکدام در یک خط، با یا بدون کپشن مشترک بعد از یک خط `---`)،
ربات همه را هم‌زمان دانلود می‌کند و بعد از اتمام دانلود همه، آن‌ها را در قالب یک آلبوم
(media group) با یک کپشن مشترک در کانال ارسال می‌کند.

### اسکرپ کردن صفحه (مثلاً صفحه‌ی یک آهنگ)

اگر لینکی بفرستید که خودش فایل مستقیم نیست بلکه یک صفحه‌ی وب است (مثل صفحه‌ی یک آهنگ یا آلبوم)،
ربات آن صفحه را بررسی می‌کند و همه‌ی لینک‌های صوتی/تصویری موجود در آن را به‌صورت دکمه‌ی شیشه‌ای
نشان می‌دهد. با زدن هرکدام، فقط همان فایل دانلود و آپلود می‌شود. این قابلیت فقط لینک‌هایی را پیدا
می‌کند که در آدرسشان پسوند فایل (مثل `.mp3`، `.mp4`) مشخص باشد.

### منوی دکمه‌ای (به‌جای تایپ دستور)

دستور `/menu` یک منوی کامل با دکمه‌های شیشه‌ای باز می‌کند: صف کارها (با دکمه لغو/توقف/تلاش دوباره
روی هر کار)، مدیریت کانال‌ها (افزودن با فوروارد کردن یک پیام از کانال، یا با آیدی/یوزرنیم؛ تعیین
پیش‌فرض؛ تغییر کپشن و فوتر؛ حذف)، تاریخچه، آمار، تنظیمات و تست پراکسی — همه بدون نیاز به تایپ هیچ
دستوری.

### دستورات مهم (در صورت تمایل به تایپ)

`/menu`، `/queue`، `/cancel <id>`، `/retry <id>`، `/history`، `/stats`، `/channels`،
`/addchannel`، `/delchannel`، `/setdefault`، `/setcaption`، `/setfooter`،
`/settings`، `/proxy`.
