"""Service configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REQUIRED_DB_VERSION = 2
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


def _telegram_backup_interval_minutes() -> int:
    minutes_raw = os.getenv("TELEGRAM_BACKUP_INTERVAL_MINUTES")
    if minutes_raw is not None and minutes_raw.strip() != "":
        return max(0, int(minutes_raw))
    hours_raw = os.getenv("TELEGRAM_BACKUP_INTERVAL_HOURS")
    if hours_raw is not None and hours_raw.strip() != "":
        return max(0, int(hours_raw) * 60)
    return 60


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw or raw == "0":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Ping samples live in their own file. 500 MiB, including the WAL sidecar.
VPN_DB_MAX_BYTES = 500 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    superadmin_id: int
    timezone: str
    db_path: Path
    backup_path: Path
    backup_interval_hours: int
    backup_keep: int
    telegram_backup_interval_minutes: int
    log_level: str
    telegram_proxy_url: str | None
    org_title: str = "Посещаемость групп"
    required_db_version: int = REQUIRED_DB_VERSION
    markers_group_id: int | None = None
    markers_export_interval_minutes: int = 10
    telegram_backup_root: Path = PROJECT_ROOT
    vpn_db_path: Path | None = None
    vpn_db_max_bytes: int = VPN_DB_MAX_BYTES
    vpn_monitor_enabled: bool = True
    vpn_monitor_interval_seconds: int = 10
    vpn_monitor_timeout_seconds: int = 8
    mihomo_api_url: str = "http://127.0.0.1:19090"
    mihomo_api_secret: str | None = None
    mihomo_proxy_group: str = "AUTO"


def load_config() -> Config:
    load_dotenv()

    token = _require("BOT_TOKEN")
    try:
        superadmin_id = int(_require("SUPERADMIN_TELEGRAM_ID"))
    except ValueError as exc:
        raise ConfigError("SUPERADMIN_TELEGRAM_ID must be an integer") from exc

    db_path = Path(os.getenv("DB_PATH", "/app/data/database.sqlite3"))
    backup_path = Path(os.getenv("BACKUP_PATH", "/app/backups"))
    vpn_db_raw = os.getenv("VPN_DB_PATH", "").strip()

    return Config(
        bot_token=token,
        superadmin_id=superadmin_id,
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
        db_path=db_path,
        backup_path=backup_path,
        backup_interval_hours=max(1, _int("BACKUP_INTERVAL_HOURS", 6)),
        backup_keep=max(1, _int("BACKUP_KEEP", 14)),
        telegram_backup_interval_minutes=_telegram_backup_interval_minutes(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        telegram_proxy_url=_optional("TELEGRAM_PROXY_URL"),
        org_title=os.getenv("ORG_TITLE", "").strip() or "Посещаемость групп",
        markers_group_id=_optional_int("MARKERS_GROUP_ID"),
        markers_export_interval_minutes=max(0, _int("MARKERS_EXPORT_INTERVAL_MINUTES", 10)),
        telegram_backup_root=Path(os.getenv("TELEGRAM_BACKUP_ROOT", "").strip() or str(PROJECT_ROOT)),
        vpn_db_path=Path(vpn_db_raw) if vpn_db_raw else db_path.with_name("vpn.sqlite3"),
        vpn_db_max_bytes=max(1, _int("VPN_DB_MAX_MB", 500)) * 1024 * 1024,
        vpn_monitor_enabled=_bool("VPN_MONITOR_ENABLED", True),
        vpn_monitor_interval_seconds=max(5, _int("VPN_MONITOR_INTERVAL_SECONDS", 10)),
        vpn_monitor_timeout_seconds=max(1, _int("VPN_MONITOR_TIMEOUT_SECONDS", 8)),
        mihomo_api_url=os.getenv("MIHOMO_API_URL", "http://127.0.0.1:19090").strip() or "http://127.0.0.1:19090",
        mihomo_api_secret=_optional("MIHOMO_API_SECRET"),
        mihomo_proxy_group=os.getenv("MIHOMO_PROXY_GROUP", "AUTO").strip() or "AUTO",
    )
