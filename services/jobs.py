"""Periodic jobs: disk backup, backup to Telegram, cleanup."""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import Config
from database.database import Database
from database.queries import Repo
from services.alerts import format_alert, notify_alert
from utils.telegram import bytes_file
from utils.time import now_utc, parse_iso, to_iso

logger = logging.getLogger(__name__)


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot, repo: Repo, db: Database, config: Config) -> None:
    scheduler.add_job(
        backup_job,
        "interval",
        hours=config.backup_interval_hours,
        id="backup",
        replace_existing=True,
        kwargs={"db": db, "bot": bot, "config": config},
        max_instances=1,
        coalesce=True,
    )
    if config.telegram_backup_interval_hours > 0:
        scheduler.add_job(
            telegram_backup_job,
            "interval",
            minutes=30,
            id="telegram_backup",
            replace_existing=True,
            kwargs={"db": db, "bot": bot, "config": config},
            max_instances=1,
            coalesce=True,
            next_run_time=now_utc() + timedelta(seconds=20),
        )
    scheduler.add_job(
        cleanup_job,
        "interval",
        hours=6,
        id="cleanup",
        replace_existing=True,
        kwargs={"repo": repo},
        max_instances=1,
        coalesce=True,
    )


async def backup_job(db: Database, bot: Bot, config: Config) -> None:
    try:
        path = await db.backup(prefix="scheduled")
        logger.info("Scheduled backup %s", path.name)
    except Exception as exc:
        logger.exception("Scheduled backup failed")
        await notify_alert(bot, config, format_alert("backup", "Не удалось сделать бэкап на диск", exc=exc))


async def send_db_snapshot(db: Database, bot: Bot, config: Config, chat_id: int, *, silent: bool) -> None:
    path = await db.backup(prefix="telegram")
    data = path.read_bytes()
    stamp = now_utc().strftime("%Y-%m-%d_%H-%M")
    await bot.send_document(
        chat_id,
        bytes_file(data, f"attendance-backup_{stamp}.sqlite3"),
        caption=f"Копия базы данных, {stamp} UTC",
        disable_notification=silent,
    )
    await db.set_system("last_telegram_backup_at", to_iso(now_utc()))


async def telegram_backup_job(db: Database, bot: Bot, config: Config) -> None:
    try:
        last_raw = await db.get_system("last_telegram_backup_at")
        if last_raw:
            last = parse_iso(last_raw)
            if now_utc() - last < timedelta(hours=config.telegram_backup_interval_hours):
                return
        await send_db_snapshot(db, bot, config, config.superadmin_id, silent=True)
        logger.info("Telegram backup sent")
    except Exception as exc:
        logger.exception("Telegram backup failed")
        await notify_alert(bot, config, format_alert("telegram_backup", "Не удалось отправить бэкап в Telegram", exc=exc))


async def cleanup_job(repo: Repo) -> None:
    try:
        await repo.cleanup_callbacks(to_iso(now_utc() - timedelta(days=2)))
    except Exception:
        logger.exception("Callback cleanup failed")
