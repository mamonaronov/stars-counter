"""Service alerts go to the superadmin. Start and stop also reach admins."""

from __future__ import annotations

import asyncio
import html
import logging
import traceback

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import InlineKeyboardMarkup

from config import Config
from database.queries import Repo
from services.roles import ROLE_ADMIN, ROLE_CHIEF_ADMIN
from utils.time import now_utc
from utils.timeouts import await_or_abandon

logger = logging.getLogger(__name__)

_NOTIFY_TIMEOUT = 15.0
# Stop notices share Docker's 30s grace with the backup, so the fan-out stays short.
_STOP_NOTIFY_TIMEOUT = 4.0
_STOP_NOTIFY_BUDGET = 8.0

BOT_STARTED_TEXT = "✅ Бот запущен и готов к работе."
BOT_STOPPED_TEXT = "⏹ Бот выключается."


def format_exception_reason(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).strip()
    if isinstance(exc, TimeoutError) and not text:
        return "превышено время ожидания"
    if not text:
        return name
    return text if text.startswith(name) else f"{name}: {text}"


def format_alert(kind: str, description: str, context: str | None = None, exc: BaseException | None = None) -> str:
    ts = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
    parts = [
        "🚨 <b>Алерт сервиса</b>",
        f"Тип: {html.escape(kind)}",
        f"Время: {ts}",
        f"Описание: {html.escape(description)}",
    ]
    if context:
        parts.append(f"Контекст: {html.escape(context)}")
    if exc is not None:
        parts.append(f"Причина: {html.escape(format_exception_reason(exc))}")
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        parts.append(f"<pre>{html.escape(tb[-1500:])}</pre>")
    return "\n".join(parts)


async def notify_superadmin(
    bot: Bot,
    config: Config,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    silent: bool = False,
) -> None:
    kwargs: dict = {"request_timeout": int(_NOTIFY_TIMEOUT), "disable_notification": silent}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    try:
        await await_or_abandon(
            bot.send_message(config.superadmin_id, text, **kwargs),
            _NOTIFY_TIMEOUT,
            name="notify_superadmin",
        )
    except TelegramForbiddenError:
        logger.error("Cannot message superadmin %s", config.superadmin_id)
    except TimeoutError:
        logger.warning("notify_superadmin timed out")
    except Exception:
        logger.exception("Failed to notify superadmin")


async def notify_alert(bot: Bot, config: Config, text: str) -> None:
    await notify_superadmin(bot, config, text)


async def lifecycle_recipient_ids(repo: Repo, config: Config) -> list[int]:
    """Superadmin, the chief admin, and every admin who has a linked Telegram."""
    seen = {config.superadmin_id}
    recipients = [config.superadmin_id]
    for student in await repo.list_students_with_roles():
        if student.role not in {ROLE_ADMIN, ROLE_CHIEF_ADMIN}:
            continue
        telegram_id = student.telegram_id
        if telegram_id is None or telegram_id in seen:
            continue
        seen.add(telegram_id)
        recipients.append(telegram_id)
    return recipients


async def notify_lifecycle(
    bot: Bot,
    repo: Repo,
    config: Config,
    text: str,
    *,
    storage: BaseStorage | None = None,
    with_menu: bool = True,
) -> None:
    """Tell admins the bot started or is stopping.

    Startup sends the notice, then the same main menu as /start, and drops any
    draft so the next message is not swallowed by an old dialog. Shutdown sends
    only the silent line: the menu would die with the process, and the next
    start clears drafts anyway. Those sends run together under a short budget
    so they fit beside the backup inside Docker's stop grace.
    """
    if not with_menu:
        try:
            await asyncio.wait_for(_fanout_stop(bot, repo, config, text), timeout=_STOP_NOTIFY_BUDGET)
        except TimeoutError:
            logger.warning("Shutdown notice exceeded %.0fs", _STOP_NOTIFY_BUDGET)
        return

    from handlers.common import menu_text
    from keyboards.main import main_menu
    from middlewares import build_actor

    open_requests = await repo.count_open_requests()
    for telegram_id in await lifecycle_recipient_ids(repo, config):
        await _clear_dialog(storage, bot, telegram_id)
        if not await _deliver(bot, telegram_id, text, silent=True):
            continue
        actor = await build_actor(repo, config, telegram_id)
        markup = main_menu(actor, open_requests=open_requests if actor.can_mark else 0)
        await _deliver(bot, telegram_id, menu_text(actor, config), silent=False, reply_markup=markup)


async def _fanout_stop(bot: Bot, repo: Repo, config: Config, text: str) -> None:
    recipients = await lifecycle_recipient_ids(repo, config)
    await asyncio.gather(
        *(
            _deliver(bot, telegram_id, text, silent=True, timeout=_STOP_NOTIFY_TIMEOUT)
            for telegram_id in recipients
        ),
        return_exceptions=True,
    )


async def _clear_dialog(storage: BaseStorage | None, bot: Bot, telegram_id: int) -> None:
    if storage is None:
        return
    try:
        bot_id = bot.id
    except Exception:
        return
    key = StorageKey(bot_id=bot_id, chat_id=telegram_id, user_id=telegram_id)
    try:
        await storage.set_state(key, None)
        await storage.set_data(key, {})
    except Exception:
        logger.exception("Could not clear dialog for %s", telegram_id)


async def _deliver(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    silent: bool,
    reply_markup: InlineKeyboardMarkup | None = None,
    timeout: float = _NOTIFY_TIMEOUT,
) -> bool:
    kwargs: dict = {"request_timeout": max(1, int(timeout)), "disable_notification": silent}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    try:
        await await_or_abandon(
            bot.send_message(chat_id, text, **kwargs),
            timeout,
            name="notify_lifecycle",
        )
        return True
    except TelegramForbiddenError:
        logger.error("Cannot message %s", chat_id)
    except TimeoutError:
        logger.warning("notify_lifecycle timed out for %s", chat_id)
    except Exception:
        logger.exception("Failed to notify %s", chat_id)
    return False
