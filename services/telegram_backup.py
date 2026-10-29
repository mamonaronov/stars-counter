"""Pack the database, configs and .env, and send a silent backup to a bound group.

The archive is sent at most once per interval, and only when stored data changed
since the previous archive delivered to that group.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile

from config import Config
from database.database import Database
from utils.app_version import app_build_identity, slugify_commit_title
from utils.formatting import seconds_human
from utils.time import now_utc, parse_iso, to_iso, to_local

logger = logging.getLogger(__name__)

TELEGRAM_DOCUMENT_LIMIT = 50 * 1024 * 1024
CONFIG_ITEMS = (
    ".env.example",
    "config.py",
    "docker-compose.yml",
    "docker-compose.proxy.yml",
    "Dockerfile",
    "docker-entrypoint.sh",
    "requirements.txt",
    "deploy",
)
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".gitignore")
LAST_SENT_KEY = "last_telegram_backup_at"
FINGERPRINT_KEY = "telegram_backup_fingerprint"
CHAT_ID_KEY = "telegram_backup_chat_id"
CHAT_TITLE_KEY = "telegram_backup_chat_title"
_VOLATILE_SYSTEM = (
    "last_backup_at",
    "last_backup_path",
    "last_telegram_backup_at",
    "telegram_backup_fingerprint",
    "markers_export_at",
    "markers_export_fingerprint",
    "markers_export_message_id",
    "markers_export_last_error",
)
_CONTENT_SQL = (
    "SELECT telegram_id, username, first_name, last_name, first_seen_at FROM users ORDER BY telegram_id",
    "SELECT * FROM students ORDER BY id",
    "SELECT * FROM verification_links ORDER BY token",
    "SELECT * FROM event_templates ORDER BY id",
    "SELECT * FROM events ORDER BY id",
    "SELECT * FROM attendance ORDER BY event_id, student_id",
    "SELECT * FROM excuses ORDER BY id",
    "SELECT * FROM requests ORDER BY id",
    "SELECT * FROM audit_log ORDER BY id",
)
_ACTIVE_MEMBER = frozenset({"member", "administrator", "creator"})
_GROUP_TYPES = frozenset({"group", "supergroup"})


class TelegramBackupError(RuntimeError):
    pass


def telegram_backup_due(
    last_sent: datetime | None,
    interval_minutes: int,
    now: datetime | None = None,
) -> bool:
    if interval_minutes <= 0:
        return False
    now = now or now_utc()
    if last_sent is None:
        return True
    return now >= last_sent + timedelta(minutes=interval_minutes)


def next_telegram_backup_at(
    last_sent: datetime | None,
    interval_minutes: int,
    now: datetime | None = None,
) -> datetime:
    now = now or now_utc()
    if last_sent is None or interval_minutes <= 0:
        return now
    due_at = last_sent + timedelta(minutes=interval_minutes)
    return now if now >= due_at else due_at


def next_backup_value(
    last_sent: datetime | None,
    interval_minutes: int,
    now: datetime | None = None,
) -> str:
    now = now or now_utc()
    if interval_minutes <= 0:
        return "выкл"
    when = next_telegram_backup_at(last_sent, interval_minutes, now)
    remaining = (when - now).total_seconds()
    if remaining <= 0:
        return "сейчас"
    return f"через {seconds_human(remaining)}"


def backup_interval_caption(interval_minutes: int) -> str:
    if interval_minutes <= 0:
        return "выкл"
    if interval_minutes == 60:
        period = "каждый час"
    elif interval_minutes % 60 == 0:
        period = f"каждые {interval_minutes // 60} ч"
    else:
        period = f"каждые {interval_minutes} мин"
    return f"{period}, без звука, только если данные изменились"


def is_backup_group_chat(chat_type: str | None) -> bool:
    return (chat_type or "") in _GROUP_TYPES


def member_is_in_chat(status: str | None, *, is_member: bool | None = None) -> bool:
    if status in _ACTIVE_MEMBER:
        return True
    if status == "restricted":
        return bool(is_member)
    return False


def membership_in_chat(member) -> bool:
    status = getattr(member, "status", None)
    if hasattr(status, "value"):
        status = status.value
    return member_is_in_chat(str(status or ""), is_member=getattr(member, "is_member", None))


def backup_group_membership_action(
    chat_type: str | None,
    *,
    chat_id: int,
    stored_id: int | None,
    old_in: bool,
    new_in: bool,
    actor_is_owner: bool,
) -> str | None:
    if not is_backup_group_chat(chat_type):
        return None
    if (not old_in) and new_in and actor_is_owner:
        return "bind"
    if old_in and (not new_in) and stored_id == chat_id:
        return "unbind"
    return None


def backup_group_bound_text(title: str | None, interval_minutes: int) -> str:
    name = f"«{html.escape(title)}»" if title else "Эта группа"
    return (
        f"📦 {name} будет получать автоматические бэкапы "
        f"({backup_interval_caption(interval_minutes)}).\n"
        "Дайте боту право отправлять файлы. В личку архив приходит только по кнопке в сервисе."
    )


def backup_group_unbound_text(title: str | None) -> str:
    name = f"«{html.escape(title)}»" if title else "группы"
    return f"Автоотправка бэкапов в {name} отключена."


async def last_telegram_backup_at(db: Database) -> datetime | None:
    raw = await db.get_system(LAST_SENT_KEY)
    if not raw:
        return None
    try:
        return parse_iso(raw)
    except ValueError:
        logger.warning("Invalid %s value: %s", LAST_SENT_KEY, raw)
        return None


async def telegram_backup_chat(db: Database) -> tuple[int | None, str | None]:
    raw = await db.get_system(CHAT_ID_KEY)
    if not raw or not raw.strip():
        return None, None
    try:
        chat_id = int(raw.strip())
    except ValueError:
        logger.warning("Invalid %s value: %s", CHAT_ID_KEY, raw)
        return None, None
    title = await db.get_system(CHAT_TITLE_KEY)
    return chat_id, title or None


async def set_telegram_backup_chat(db: Database, chat_id: int, title: str | None) -> None:
    await db.set_system(CHAT_ID_KEY, str(int(chat_id)))
    await db.set_system(CHAT_TITLE_KEY, (title or "").strip())


async def clear_telegram_backup_chat(db: Database) -> None:
    await db.set_system(CHAT_ID_KEY, "")
    await db.set_system(CHAT_TITLE_KEY, "")


async def content_fingerprint(db: Database) -> str:
    """Hash of stored data. Bookkeeping timestamps and open dialogs are ignored."""
    digest = hashlib.sha256()
    placeholders = ",".join("?" for _ in _VOLATILE_SYSTEM)
    queries = (
        *_CONTENT_SQL,
        f"SELECT key, value FROM system_info WHERE key NOT IN ({placeholders}) ORDER BY key",
    )
    params: tuple = ()
    async with db.exclusive():
        for sql in queries:
            bind = _VOLATILE_SYSTEM if "system_info" in sql else params
            async with db.conn.execute(sql, bind) as cur:
                rows = await cur.fetchall()
            digest.update(sql.encode())
            for row in rows:
                digest.update(repr(tuple(row)).encode())
                digest.update(b"\n")
    return digest.hexdigest()


def backup_archive_name(started: datetime, commit: str, title: str, db_version: int, tz_name: str) -> str:
    stamp = to_local(started, tz_name).strftime("%d-%m-%Y_%H-%M-%S")
    short = re.sub(r"[^A-Za-z0-9]", "", commit)[:12] or "unknown"
    slug = slugify_commit_title(title)
    return f"stars-counter-backup_{stamp}_{short}_{slug}_db{db_version}.tgz"


def _copy_item(src: Path, dest: Path) -> bool:
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=_IGNORE)
        return True
    shutil.copy2(src, dest)
    return True


def _env_keys_from_example(example: Path) -> list[str]:
    if not example.is_file():
        return []
    keys: list[str] = []
    for line in example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0])
    return keys


def write_env_snapshot(dest: Path, example: Path | None = None) -> None:
    keys = _env_keys_from_example(example) if example else []
    if not keys:
        keys = ["BOT_TOKEN", "SUPERADMIN_TELEGRAM_ID", "TIMEZONE", "DB_PATH", "BACKUP_PATH", "TELEGRAM_PROXY_URL"]
    lines = ["# Snapshot of runtime environment", ""]
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{key}={os.environ.get(key, '')}")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_dotenv(staging: Path, root: Path) -> str:
    dest = staging / ".env"
    for src in (root / ".env", Path("/app/.env.runtime")):
        try:
            if src.is_file():
                shutil.copy2(src, dest)
                return "file"
        except OSError:
            logger.warning("Cannot read %s, trying next source", src)
    write_env_snapshot(dest, root / ".env.example")
    return "snapshot"


def collect_configs(staging: Path, root: Path) -> list[str]:
    packed: list[str] = []
    configs_dir = staging / "configs"
    for name in CONFIG_ITEMS:
        src = root / name
        try:
            if _copy_item(src, configs_dir / name):
                packed.append(name)
        except OSError:
            logger.warning("Skipped config %s", src)
    return packed


def write_tar_gzip(src_dir: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            files = sorted(path for path in src_dir.rglob("*") if path.is_file())
            for path in files:
                tar.add(path, arcname=path.relative_to(src_dir).as_posix())
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


async def create_telegram_archive(db: Database, config: Config) -> Path:
    started = now_utc()
    db_version = await db.user_version()
    commit, title = app_build_identity()
    archive_name = backup_archive_name(started, commit, title, db_version, config.timezone)
    archive_path = config.backup_path / archive_name
    root = config.telegram_backup_root
    staging = Path(tempfile.mkdtemp(prefix="tg-backup-"))
    try:
        await db.backup_to(staging / "database.sqlite3")
        env_source = add_dotenv(staging, root)
        packed = collect_configs(staging, root)
        logger.info(
            "Telegram backup packing env=%s configs=%s commit=%s db=%s",
            env_source,
            ",".join(packed) or "-",
            commit,
            db_version,
        )
        await asyncio.to_thread(write_tar_gzip, staging, archive_path)
        return archive_path
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def send_telegram_backup(
    db: Database,
    bot: Bot,
    config: Config,
    *,
    silent: bool = True,
    chat_id: int | None = None,
    force: bool = False,
) -> Path | None:
    """Send an archive. Returns None when the group already has this data."""
    stored_id, _title = await telegram_backup_chat(db)
    dest = chat_id if chat_id is not None else stored_id
    if dest is None:
        raise TelegramBackupError("Backup group is not set")
    recording = stored_id is not None and dest == stored_id
    fingerprint = await content_fingerprint(db)
    if recording and not force:
        previous = await db.get_system(FINGERPRINT_KEY)
        if previous == fingerprint:
            logger.info("Telegram backup skipped: data unchanged")
            return None
    path = await create_telegram_archive(db, config)
    size = path.stat().st_size
    if size > TELEGRAM_DOCUMENT_LIMIT:
        raise TelegramBackupError(
            f"Archive {path.name} is {size} bytes, over Telegram limit {TELEGRAM_DOCUMENT_LIMIT}"
        )
    commit, title = app_build_identity()
    db_version = await db.user_version()
    extra = "\nОтправлен вручную из сервиса" if not silent else ""
    caption = (
        "📦 <b>Резервная копия</b>\n"
        f"<code>{html.escape(path.name)}</code>\n"
        f"Коммит: {html.escape(title)} (<code>{html.escape(commit)}</code>)\n"
        f"БД: v{db_version}\n"
        "БД + конфиги + .env"
        f"{extra}"
    )
    try:
        await bot.send_document(
            dest,
            FSInputFile(path, filename=path.name),
            caption=caption,
            disable_notification=silent,
            request_timeout=120,
        )
    except TelegramForbiddenError:
        if stored_id is not None and stored_id == dest:
            await clear_telegram_backup_chat(db)
            logger.warning("Backup group %s is gone, binding cleared", dest)
        logger.exception("Failed to send telegram backup %s, file kept at %s", path.name, path)
        raise
    except Exception:
        logger.exception("Failed to send telegram backup %s, file kept at %s", path.name, path)
        raise
    path.unlink(missing_ok=True)
    if recording:
        await db.set_system(LAST_SENT_KEY, to_iso(now_utc()))
        await db.set_system(FINGERPRINT_KEY, fingerprint)
    logger.info("Telegram backup sent %s (%s bytes) silent=%s chat=%s", path.name, size, silent, dest)
    return path
