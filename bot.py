"""Attendance bot entry point: one process, one Bot(), long polling."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import ConfigError, load_config
from database.database import Database, DatabaseUnrecoverableError
from database.fsm_storage import SqliteStorage
from database.queries import Repo
from handlers import setup_routers
from middlewares import ActorMiddleware, CallbackIdempotencyMiddleware, ContextMiddleware, ErrorIsolationMiddleware
from services.alerts import BOT_STARTED_TEXT, BOT_STOPPED_TEXT, format_alert, notify_alert, notify_superadmin
from services.jobs import setup_scheduler
from utils.logging import setup_logging
from utils.runtime import RuntimeControl, set_runtime
from utils.telegram import is_stale_callback_error, is_transient_telegram_error
from utils.telegram_session import make_telegram_session
from utils.timeouts import await_or_abandon, reset_bot_session

logger = logging.getLogger("bot")

_POLLING_RETRY_INITIAL = 2.0
_POLLING_RETRY_MAX = 30.0
_POLLING_TIMEOUT = 20
_POLLING_SESSION_TIMEOUT = 25.0
_STARTUP_GET_ME_TIMEOUT = 20.0


def _make_session(proxy_url: str | None):
    return make_telegram_session(proxy_url, _POLLING_SESSION_TIMEOUT)


def _bot_session(proxy_url: str | None):
    try:
        session = _make_session(proxy_url)
    except ImportError as exc:
        raise ConfigError("TELEGRAM_PROXY_URL is set but aiohttp-socks is not installed") from exc
    logger.info("telegram_proxy_enabled" if proxy_url else "telegram_proxy_disabled")
    return session


async def _recycle_bot_session(bot: Bot, proxy_url: str | None) -> None:
    await reset_bot_session(bot)
    try:
        bot.session = _make_session(proxy_url)
    except Exception:
        logger.exception("Failed to recreate bot session")


async def _wait_until_telegram_ready(bot: Bot, stop: asyncio.Event, proxy_url: str | None) -> bool:
    delay = _POLLING_RETRY_INITIAL
    while not stop.is_set():
        try:
            await await_or_abandon(bot.me(), _STARTUP_GET_ME_TIMEOUT, name="startup.get_me")
            logger.info("Telegram API reachable")
            return True
        except (TimeoutError, TelegramNetworkError) as exc:
            logger.warning("Telegram not reachable, retry in %.0fs: %s", delay, exc)
        except Exception:
            logger.exception("Telegram handshake failed")
        await _recycle_bot_session(bot, proxy_url)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return False
        except asyncio.TimeoutError:
            delay = min(delay * 2, _POLLING_RETRY_MAX)
    return False


async def _start_polling_with_retry(dp: Dispatcher, bot: Bot, stop: asyncio.Event, *, proxy_url: str | None) -> None:
    delay = _POLLING_RETRY_INITIAL
    while not stop.is_set():
        try:
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                handle_signals=False,
                close_bot_session=False,
                polling_timeout=_POLLING_TIMEOUT,
            )
            return
        except (TelegramNetworkError, TimeoutError) as exc:
            logger.warning("Telegram network error, retry in %.0fs: %s", delay, exc)
            await _recycle_bot_session(bot, proxy_url)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                delay = min(delay * 2, _POLLING_RETRY_MAX)


async def graceful_shutdown(bot: Bot, db: Database, scheduler: AsyncIOScheduler, config, *, notify: bool) -> None:
    logger.info("Graceful shutdown started")
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("Scheduler shutdown failed")
    try:
        await db.backup(prefix="shutdown")
    except Exception as exc:
        logger.exception("Shutdown backup failed")
        if notify:
            await notify_alert(bot, config, format_alert("backup", "Не удалось сделать бэкап при выключении", exc=exc))
    if notify:
        await notify_superadmin(bot, config, BOT_STOPPED_TEXT, silent=True)
    try:
        await bot.session.close()
    except Exception:
        logger.exception("Bot session close failed")
    try:
        await db.close()
    except Exception:
        logger.exception("DB close failed")
    logger.info("Shutdown complete")


async def run() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    setup_logging(config.log_level)
    logger.info("Starting attendance bot")

    try:
        session = _bot_session(config.telegram_proxy_url)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    db = Database(config)
    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)

    try:
        await db.initialize()
    except DatabaseUnrecoverableError as exc:
        logger.critical("Database unrecoverable: %s", exc)
        await notify_alert(bot, config, format_alert("database", str(exc), exc=exc))
        await bot.session.close()
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.exception("Startup database failure")
        await notify_alert(bot, config, format_alert("startup", "Ошибка инициализации БД", exc=exc))
        await bot.session.close()
        raise SystemExit(1) from exc

    repo = Repo(db)
    dp = Dispatcher(storage=SqliteStorage(db))
    scheduler = AsyncIOScheduler(timezone="UTC")
    runtime = RuntimeControl()
    set_runtime(runtime)

    dp.update.outer_middleware(ContextMiddleware(repo, config, bot, scheduler))
    dp.update.outer_middleware(ErrorIsolationMiddleware())
    dp.update.outer_middleware(ActorMiddleware())
    dp.callback_query.outer_middleware(CallbackIdempotencyMiddleware())
    dp.include_router(setup_routers())

    @dp.error()
    async def dispatcher_error(event) -> None:
        exc = event.exception
        if is_transient_telegram_error(exc):
            logger.warning("Dispatcher skipped a Telegram network error: %s", exc)
            return
        if is_stale_callback_error(exc):
            return
        logger.exception("Dispatcher error")
        await notify_alert(bot, config, format_alert("dispatcher", "Ошибка диспетчера", exc=exc))

    loop = asyncio.get_running_loop()
    runtime.bind(lambda: loop.create_task(dp.stop_polling()))
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, runtime.request_stop)
        except NotImplementedError:
            pass

    logger.info("Waiting for Telegram API")
    if not await _wait_until_telegram_ready(bot, runtime.stop, config.telegram_proxy_url) or runtime.stop.is_set():
        set_runtime(None)
        await graceful_shutdown(bot, db, scheduler, config, notify=False)
        return

    try:
        setup_scheduler(scheduler, bot, repo, db, config)
        scheduler.start()
    except Exception as exc:
        logger.exception("Failed to start background jobs")
        await notify_alert(bot, config, format_alert("scheduler", "Ошибка запуска задач", exc=exc))

    became_ready = False

    @dp.startup.register
    async def _on_bot_ready() -> None:
        nonlocal became_ready
        if became_ready:
            return
        became_ready = True
        logger.info("Polling started")
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Главное меню"),
                    BotCommand(command="menu", description="Главное меню"),
                    BotCommand(command="help", description="Как пользоваться ботом"),
                ]
            )
        except Exception:
            logger.exception("Failed to set bot commands")
        await notify_superadmin(bot, config, BOT_STARTED_TEXT, silent=True)

    try:
        await _start_polling_with_retry(dp, bot, runtime.stop, proxy_url=config.telegram_proxy_url)
    finally:
        set_runtime(None)
        await graceful_shutdown(bot, db, scheduler, config, notify=became_ready)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception:
        logging.getLogger("bot").exception("Fatal error")
        raise SystemExit(1)
