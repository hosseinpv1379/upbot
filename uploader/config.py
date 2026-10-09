"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProxyConfig:
    """Parsed proxy settings usable by python-socks / Telethon / aiohttp-socks."""

    __slots__ = ("scheme", "host", "port", "username", "password", "rdns")

    def __init__(self, url: str, rdns: bool = True) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("socks5", "socks4", "http"):
            raise ValueError(f"Unsupported proxy scheme: {parsed.scheme}")
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"Proxy URL must include host and port: {url}")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port
        self.username = parsed.username
        self.password = parsed.password
        self.rdns = rdns

    def as_telethon_tuple(self) -> tuple:
        """Return the (socks module type, host, port, rdns, user, pass) tuple Telethon expects."""
        import python_socks

        type_map = {
            "socks5": python_socks.ProxyType.SOCKS5,
            "socks4": python_socks.ProxyType.SOCKS4,
            "http": python_socks.ProxyType.HTTP,
        }
        return (
            type_map[self.scheme],
            self.host,
            self.port,
            self.rdns,
            self.username,
            self.password,
        )

    def as_aiohttp_socks_url(self) -> str:
        auth = ""
        if self.username:
            auth = self.username
            if self.password:
                auth += f":{self.password}"
            auth += "@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"ProxyConfig({self.scheme}://{self.host}:{self.port})"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    api_id: int = Field(..., alias="API_ID")
    api_hash: str = Field(..., alias="API_HASH")
    bot_token: str = Field(..., alias="BOT_TOKEN")
    user_session: Optional[str] = Field(default=None, alias="USER_SESSION")

    admin_ids: str = Field(..., alias="ADMIN_IDS")

    tg_proxy: Optional[str] = Field(default=None, alias="TG_PROXY")
    dl_proxy: Optional[str] = Field(default=None, alias="DL_PROXY")

    download_dir: Path = Field(default=Path("downloads"), alias="DOWNLOAD_DIR")
    db_path: Path = Field(default=Path("uploader.db"), alias="DB_PATH")
    log_dir: Path = Field(default=Path("logs"), alias="LOG_DIR")

    max_file_size_gb: float = Field(default=2.0, alias="MAX_FILE_SIZE_GB")
    download_concurrency: int = Field(default=2, alias="DOWNLOAD_CONCURRENCY")
    upload_concurrency: int = Field(default=1, alias="UPLOAD_CONCURRENCY")
    segmented_download_connections: int = Field(default=4, alias="SEGMENTED_DOWNLOAD_CONNECTIONS")
    fast_upload_connections: int = Field(default=4, alias="FAST_UPLOAD_CONNECTIONS")

    progress_update_interval: float = Field(default=4.0, alias="PROGRESS_UPDATE_INTERVAL")
    caption_limit_default: int = Field(default=1024, alias="CAPTION_LIMIT_DEFAULT")
    caption_limit_premium: int = Field(default=4096, alias="CAPTION_LIMIT_PREMIUM")

    user_agent: str = Field(
        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Uploader/1.0",
        alias="USER_AGENT",
    )

    @field_validator("admin_ids")
    @classmethod
    def _non_empty_admins(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("ADMIN_IDS must contain at least one Telegram user id")
        return v

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x.strip()) for x in self.admin_ids.split(",") if x.strip()]

    @property
    def max_file_size_bytes(self) -> int:
        return int(self.max_file_size_gb * 1024**3)

    @property
    def tg_proxy_config(self) -> Optional[ProxyConfig]:
        return ProxyConfig(self.tg_proxy) if self.tg_proxy else None

    @property
    def dl_proxy_config(self) -> Optional[ProxyConfig]:
        if self.dl_proxy:
            return ProxyConfig(self.dl_proxy)
        return self.tg_proxy_config


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
