"""Service alerts and lifecycle notices go to the superadmin."""

from __future__ import annotations

import html
import logging
import traceback

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup

from config import Config
from utils.time import now_utc
from utils.timeouts import await_or_abandon

logger = logging.getLogger(__name__)

_NOTIFY_TIMEOUT = 15.0

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
