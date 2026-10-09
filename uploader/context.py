"""The single mutable application context shared across handlers and the job queue."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from telethon import TelegramClient

from uploader.config import Settings
from uploader.db import Database

if TYPE_CHECKING:
    from uploader.queue import JobManager

DEFAULT_UPLOAD_LIMIT = 2 * 1024**3
PREMIUM_UPLOAD_LIMIT = 4 * 1024**3


@dataclass
class AppContext:
    settings: Settings
    db: Database
    bot: TelegramClient
    user_client: Optional[TelegramClient]
    job_manager: "JobManager" = None  # type: ignore[assignment]
    upload_limit_bytes: int = DEFAULT_UPLOAD_LIMIT
    start_time: float = 0.0
    pending_batches: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.start_time:
            self.start_time = time.monotonic()

    @property
    def upload_client(self) -> TelegramClient:
        return self.user_client if self.user_client is not None else self.bot

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.start_time

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_id_list
