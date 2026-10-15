"""Service configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REQUIRED_DB_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    superadmin_id: int
    timezone: str
    db_path: Path
    backup_path: Path
    backup_interval_hours: int
    backup_keep: int
    telegram_backup_interval_hours: int
    log_level: str
    telegram_proxy_url: str | None
    org_title: str = "Посещаемость групп"
    required_db_version: int = REQUIRED_DB_VERSION


def load_config() -> Config:
    load_dotenv()

    token = _require("BOT_TOKEN")
    try:
        superadmin_id = int(_require("SUPERADMIN_TELEGRAM_ID"))
    except ValueError as exc:
        raise ConfigError("SUPERADMIN_TELEGRAM_ID must be an integer") from exc

    db_path = Path(os.getenv("DB_PATH", "/app/data/database.sqlite3"))
    backup_path = Path(os.getenv("BACKUP_PATH", "/app/backups"))

    return Config(
        bot_token=token,
        superadmin_id=superadmin_id,
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
        db_path=db_path,
        backup_path=backup_path,
        backup_interval_hours=max(1, _int("BACKUP_INTERVAL_HOURS", 6)),
        backup_keep=max(1, _int("BACKUP_KEEP", 14)),
        telegram_backup_interval_hours=max(0, _int("TELEGRAM_BACKUP_INTERVAL_HOURS", 24)),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        telegram_proxy_url=_optional("TELEGRAM_PROXY_URL"),
        org_title=os.getenv("ORG_TITLE", "").strip() or "Посещаемость групп",
    )
