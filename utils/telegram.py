"""Safe Telegram message helpers."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_transient_telegram_error(exc: BaseException) -> bool:
    return isinstance(exc, (TelegramNetworkError, TimeoutError))


def _failed_before_telegram_answered(exc: BaseException) -> bool:
    """The HTTP call died locally. A timeout may already have been accepted, so it is not retried."""
    if not isinstance(exc, TelegramNetworkError):
        return False
    return "timeout" not in str(exc).lower()


async def retry_unanswered_call(start: Callable[[], Awaitable[T]], *, name: str) -> T:
    """Run start once more when the first attempt never got a Telegram response."""
    try:
        return await start()
    except TelegramNetworkError as exc:
        if not _failed_before_telegram_answered(exc):
            raise
        logger.warning("%s did not reach Telegram (%s), retrying once", name, exc)
        return await start()


async def retry_file_send(sender: Callable[..., Awaitable[T]], /, *args: Any, name: str, **kwargs: Any) -> T:
    """Send a file, and try once more if Telegram never answered the first upload."""
    return await retry_unanswered_call(lambda: sender(*args, **kwargs), name=name)


def is_stale_callback_error(exc: BaseException) -> bool:
    if not isinstance(exc, TelegramBadRequest):
        return False
    text = str(exc).lower()
    return "query is too old" in text or "query id is invalid" in text


_answered: OrderedDict[str, None] = OrderedDict()
_ANSWERED_KEEP = 2000


async def answer_callback(cb: CallbackQuery, text: str | None = None, *, show_alert: bool = False) -> None:
    """Ack a button tap once. Repeated calls for the same query are no-ops unless they carry text."""
    key = cb.id
    if key in _answered and not text:
        return
    _answered[key] = None
    while len(_answered) > _ANSWERED_KEEP:
        _answered.popitem(last=False)
    try:
        if text:
            await cb.answer(text, show_alert=show_alert)
        else:
            await cb.answer()
    except TelegramBadRequest as exc:
        if is_stale_callback_error(exc):
            return
        raise


async def safe_edit(
    message: Message | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    if message is None:
        return None
    try:
        return await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return message
        try:
            return await message.answer(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to send fallback message")
            return None
    except Exception:
        logger.exception("Failed to edit message")
        try:
            return await message.answer(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to send fallback message")
            return None


async def safe_edit_markup(message: Message | None, reply_markup: InlineKeyboardMarkup | None) -> None:
    if message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Failed to edit markup: %s", exc)
    except Exception:
        logger.exception("Failed to edit markup")


async def safe_send_message(bot, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None, **kwargs) -> Message | None:
    try:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
    except TelegramRetryAfter as exc:
        logger.warning("Flood wait: %s", exc.retry_after)
    except TelegramForbiddenError:
        logger.info("User %s blocked the bot", chat_id)
    except TelegramBadRequest as exc:
        logger.warning("Bad request while sending to %s: %s", chat_id, exc)
    except Exception:
        logger.exception("Send failed")
    return None


def bytes_file(data: bytes, filename: str) -> BufferedInputFile:
    return BufferedInputFile(data, filename=filename)


def png_file(data: bytes, filename: str = "chart.png") -> BufferedInputFile:
    return BufferedInputFile(data, filename=filename)


def text_file(data: str, filename: str = "log.txt") -> BufferedInputFile:
    return BufferedInputFile(data.encode("utf-8"), filename=filename)


async def safe_send(sender: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any | None:
    try:
        return await sender(*args, **kwargs)
    except TelegramRetryAfter as exc:
        logger.warning("Flood wait: %s", exc.retry_after)
        return None
    except TelegramForbiddenError:
        raise
    except TelegramBadRequest:
        logger.exception("Bad request while sending")
        return None
    except Exception:
        logger.exception("Send failed")
        return None
