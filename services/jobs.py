"""Periodic jobs: disk backup, backup to Telegram, cleanup."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import Config
from database.database import Database
from database.queries import Repo
from services.alerts import format_alert, notify_alert
from services.group_export import KEY_LAST_ERROR, publish_markers_export
from utils.time import now_utc, to_iso
from utils.timeouts import await_or_abandon

logger = logging.getLogger(__name__)

_TELEGRAM_BACKUP_START_DELAY = timedelta(seconds=20)
_TELEGRAM_BACKUP_RETRY = timedelta(minutes=15)
_VPN_MONITOR_JOB_SLACK = 5.0
_VPN_RETENTION_EVERY = timedelta(minutes=15)
_LOAD_SAMPLE_INTERVAL_SECONDS = 60


def reschedule_telegram_backup(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    db: Database,
    config: Config,
    last_sent: datetime | None,
) -> None:
    if config.telegram_backup_interval_minutes <= 0:
        try:
            scheduler.remove_job("telegram_backup")
        except Exception:
            pass
        return
    from services.telegram_backup import next_telegram_backup_at

    when = next_telegram_backup_at(last_sent, config.telegram_backup_interval_minutes)
    _schedule_telegram_backup_at(scheduler, bot, db, config, when)


def _schedule_telegram_backup_at(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    db: Database,
    config: Config,
    when: datetime,
) -> None:
    grace = max(3600, config.telegram_backup_interval_minutes * 60)
    scheduler.add_job(
        telegram_backup_job,
        "date",
        run_date=when,
        id="telegram_backup",
        replace_existing=True,
        kwargs={"scheduler": scheduler, "db": db, "bot": bot, "config": config},
        misfire_grace_time=grace,
        next_run_time=when,
    )


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
    if config.telegram_backup_interval_minutes > 0:
        _schedule_telegram_backup_at(
            scheduler,
            bot,
            db,
            config,
            now_utc() + _TELEGRAM_BACKUP_START_DELAY,
        )
    if config.markers_export_interval_minutes > 0:
        scheduler.add_job(
            markers_export_job,
            "interval",
            minutes=config.markers_export_interval_minutes,
            id="markers_export",
            replace_existing=True,
            kwargs={"repo": repo, "bot": bot, "config": config},
            max_instances=1,
            coalesce=True,
            next_run_time=now_utc() + timedelta(seconds=40),
        )
    scheduler.add_job(
        load_sample_job,
        "interval",
        seconds=_LOAD_SAMPLE_INTERVAL_SECONDS,
        id="load_sample",
        replace_existing=True,
        kwargs={"db": db},
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_LOAD_SAMPLE_INTERVAL_SECONDS,
        next_run_time=now_utc() + timedelta(seconds=15),
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
    if config.vpn_monitor_enabled:
        from services.vpn_monitor import VpnMonitor, make_probe_bot

        monitor = VpnMonitor(config, probe_bot=make_probe_bot(config))
        interval = max(5, config.vpn_monitor_interval_seconds)
        scheduler.add_job(
            vpn_monitor_job,
            "interval",
            seconds=interval,
            id="vpn_monitor",
            replace_existing=True,
            kwargs={"monitor": monitor, "bot": bot, "repo": repo},
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(1, interval - 1),
            next_run_time=now_utc() + timedelta(seconds=interval),
        )
        logger.info(
            "VPN monitor enabled interval=%ss group=%s",
            interval,
            config.mihomo_proxy_group,
        )
    scheduler.add_job(
        vpn_retention_job,
        "interval",
        minutes=15,
        id="vpn_retention",
        replace_existing=True,
        kwargs={"db": db},
        max_instances=1,
        coalesce=True,
        next_run_time=now_utc() + _VPN_RETENTION_EVERY,
    )


async def backup_job(db: Database, bot: Bot, config: Config) -> None:
    try:
        path = await db.backup(prefix="scheduled")
        logger.info("Scheduled backup %s", path.name)
    except Exception as exc:
        logger.exception("Scheduled backup failed")
        await notify_alert(bot, config, format_alert("backup", "Не удалось сделать бэкап на диск", exc=exc))


async def telegram_backup_job(scheduler: AsyncIOScheduler, db: Database, bot: Bot, config: Config) -> None:
    from services.telegram_backup import (
        last_telegram_backup_at,
        next_telegram_backup_at,
        send_telegram_backup,
        telegram_backup_chat,
        telegram_backup_due,
    )

    interval = config.telegram_backup_interval_minutes
    try:
        last = await last_telegram_backup_at(db)
        if not telegram_backup_due(last, interval):
            when = next_telegram_backup_at(last, interval)
            logger.info("Telegram backup not due, next at %s", when.isoformat())
        else:
            chat_id, _title = await telegram_backup_chat(db)
            if chat_id is None:
                logger.info("Telegram backup skipped: group is not bound")
            else:
                sent = await send_telegram_backup(db, bot, config, silent=True)
                if sent is None:
                    logger.info("Telegram backup skipped: no changes")
                else:
                    logger.info("Telegram backup sent")
            when = now_utc() + timedelta(minutes=max(interval, 1))
        _schedule_telegram_backup_at(scheduler, bot, db, config, when)
    except Exception as exc:
        logger.exception("Telegram backup failed")
        await notify_alert(
            bot,
            config,
            format_alert("telegram_backup", "Не удалось сделать или отправить бэкап в Telegram", exc=exc),
        )
        _schedule_telegram_backup_at(scheduler, bot, db, config, now_utc() + _TELEGRAM_BACKUP_RETRY)


async def markers_export_job(repo: Repo, bot: Bot, config: Config) -> None:
    try:
        result = await publish_markers_export(bot, repo, config, silent=True)
    except Exception as exc:
        logger.exception("Markers export failed")
        signature = f"{type(exc).__name__}: {exc}"[:500]
        last = await repo.db.get_system(KEY_LAST_ERROR)
        if last == signature:
            return
        await repo.db.set_system(KEY_LAST_ERROR, signature)
        await notify_alert(
            bot,
            config,
            format_alert("markers_export", "Не удалось отправить таблицу посещаемости в группу", exc=exc),
        )
        return
    if result == "sent":
        logger.info("Attendance table sent to the markers group")


async def cleanup_job(repo: Repo) -> None:
    try:
        await repo.cleanup_callbacks(to_iso(now_utc() - timedelta(days=2)))
    except Exception:
        logger.exception("Callback cleanup failed")
    try:
        load_db = repo.db.load_db
        if load_db is not None:
            deleted = await load_db.prune_retained(repo.db.config.load_log_keep_days)
            if deleted:
                logger.info("Load samples pruned: %s", deleted)
    except Exception:
        logger.exception("Load sample prune failed")


async def load_sample_job(db: Database) -> None:
    try:
        from services.load_samples import capture_server_load

        await capture_server_load(db)
    except Exception:
        logger.exception("Load average sample failed")


def vpn_monitor_job_timeout(monitor) -> float:
    """Keep the APScheduler slot shorter than the interval so ticks are not skipped."""
    inner = float(monitor.config.vpn_monitor_timeout_seconds) + _VPN_MONITOR_JOB_SLACK
    interval = max(5, int(monitor.config.vpn_monitor_interval_seconds))
    return min(inner, max(1.0, interval - 1.0))


async def vpn_monitor_job(monitor, bot: Bot, repo: Repo) -> None:
    timeout = vpn_monitor_job_timeout(monitor)
    try:
        await await_or_abandon(monitor.tick(bot, repo), timeout, name="vpn_monitor_job")
    except TimeoutError:
        logger.warning("VPN monitor tick timed out after %.0fs, resetting probe session", timeout)
        await monitor.reset_probe()
    except Exception:
        logger.exception("VPN monitor tick failed")


async def vpn_retention_job(db: Database) -> None:
    vpn = db.vpn_db
    if vpn is None:
        return
    try:
        deleted = await vpn.enforce_max_bytes(db.config.vpn_db_max_bytes)
        if deleted:
            logger.info("VPN samples pruned: %s", deleted)
    except Exception:
        logger.exception("VPN sample prune failed")
