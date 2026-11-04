"""Remember the last private-chat screen and replace it when a new build starts.

Telegram keeps old messages. The bot can edit only a message whose id it stored.
On a new commit that screen becomes the current menu and the unfinished dialog
is dropped, so the next typed text is not swallowed by the previous step.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.storage.base import BaseStorage

from config import Config
from database.database import Database
from database.queries import Repo
from services.alerts import _clear_dialog
from utils.app_version import app_build_identity
from utils.telegram import is_transient_telegram_error
from utils.time import now_utc, to_iso

logger = logging.getLogger(__name__)

SCREEN_COMMIT_KEY = "screens_refreshed_commit"
_TRACKED_METHODS = frozenset({"sendMessage", "editMessageText"})

_db: Database | None = None


def bind_screen_memory(db: Database | None) -> None:
    global _db
    _db = db


def attach_screen_memory(session) -> None:
    session.middleware.register(screen_memory_middleware)


async def screen_memory_middleware(make_request, bot: Bot, method) -> Any:
    result = await make_request(bot, method)
    db = _db
    if db is None:
        return result
    chat_id, message_id = _screen_target(method, result)
    if chat_id is None or message_id is None:
        return result
    if db._lock.locked():
        task = asyncio.create_task(_remember_later(db, chat_id, message_id))
        task.add_done_callback(_log_remember)
        return result
    await _remember_later(db, chat_id, message_id)
    return result


def _log_remember(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Could not remember private screen: %s", exc)


async def _remember_later(db: Database, chat_id: int, message_id: int) -> None:
    try:
        await remember_private_screen(db, chat_id, message_id)
    except Exception:
        logger.exception("Could not remember private screen chat=%s message=%s", chat_id, message_id)


def _screen_target(method, result) -> tuple[int | None, int | None]:
    api = getattr(method, "__api_method__", "")
    if api not in _TRACKED_METHODS:
        return None, None
    if result is True or result is None:
        return None, None
    # Plain notices ("бот запущен", ошибки без кнопок) are not the screen the
    # user is navigating. Remembering them would hide the real menu.
    if api == "sendMessage" and getattr(result, "reply_markup", None) is None and getattr(method, "reply_markup", None) is None:
        return None, None
    chat = getattr(result, "chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else getattr(method, "chat_id", None)
    message_id = getattr(result, "message_id", None)
    if message_id is None:
        message_id = getattr(method, "message_id", None)
    try:
        return int(chat_id), int(message_id)
    except (TypeError, ValueError):
        return None, None


async def remember_private_screen(db: Database, chat_id: int, message_id: int) -> None:
    """Store the message currently on screen in a private chat. Groups are ignored."""
    if chat_id <= 0 or message_id <= 0:
        return
    async with db.exclusive():
        await db.conn.execute(
            """
            INSERT INTO chat_screens (chat_id, message_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                message_id = excluded.message_id,
                updated_at = excluded.updated_at
            """,
            (int(chat_id), int(message_id), to_iso(now_utc())),
        )
        await db.conn.commit()


async def forget_private_screen(db: Database, chat_id: int) -> None:
    async with db.exclusive():
        await db.conn.execute("DELETE FROM chat_screens WHERE chat_id = ?", (int(chat_id),))
        await db.conn.commit()


async def private_screens(db: Database) -> list[tuple[int, int]]:
    async with db.exclusive():
        async with db.conn.execute("SELECT chat_id, message_id FROM chat_screens ORDER BY chat_id") as cur:
            rows = await cur.fetchall()
    return [(int(row[0]), int(row[1])) for row in rows]


async def refresh_private_screens(
    bot: Bot,
    repo: Repo,
    config: Config,
    storage: BaseStorage | None,
) -> set[int]:
    """Edit remembered screens to the current menu when the build commit changed.

    Returns chat ids whose screen now shows that menu. A network failure leaves
    the stored commit unchanged, so the next start tries again.
    """
    commit, _title = app_build_identity()
    if commit in {"", "unknown"}:
        return set()
    if await repo.db.get_system(SCREEN_COMMIT_KEY) == commit:
        return set()
    refreshed: set[int] = set()
    incomplete = False
    for chat_id, message_id in await private_screens(repo.db):
        try:
            if await _refresh_one(bot, repo, config, storage, chat_id, message_id):
                refreshed.add(chat_id)
        except Exception as exc:
            if isinstance(exc, TelegramRetryAfter) or is_transient_telegram_error(exc):
                incomplete = True
                logger.warning("Screen refresh stopped at chat %s: %s", chat_id, exc)
                break
            logger.exception("Screen refresh failed for chat %s", chat_id)
    if not incomplete:
        await repo.db.set_system(SCREEN_COMMIT_KEY, commit)
        logger.info("Private screens refreshed for %s: %s", commit, len(refreshed))
    return refreshed


async def _menu_for(repo: Repo, config: Config, telegram_id: int):
    from handlers.common import menu_text
    from keyboards.main import main_menu
    from middlewares import build_actor

    actor = await build_actor(repo, config, telegram_id)
    open_requests = await repo.count_open_requests() if actor.can_mark else 0
    return menu_text(actor, config), main_menu(actor, open_requests=open_requests)


async def _refresh_one(
    bot: Bot,
    repo: Repo,
    config: Config,
    storage: BaseStorage | None,
    chat_id: int,
    message_id: int,
) -> bool:
    text, markup = await _menu_for(repo, config, chat_id)
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            sent = await _send_replacement(bot, repo.db, chat_id, text, markup)
            if sent is None:
                return False
    except TelegramForbiddenError:
        await forget_private_screen(repo.db, chat_id)
        return False
    await _clear_dialog(storage, bot, chat_id)
    return True


async def _send_replacement(bot: Bot, db: Database, chat_id: int, text: str, markup) -> Any | None:
    try:
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramForbiddenError:
        await forget_private_screen(db, chat_id)
        return None
    except TelegramBadRequest:
        logger.warning("Cannot replace screen in chat %s", chat_id)
        await forget_private_screen(db, chat_id)
        return None
    message_id = getattr(sent, "message_id", None)
    if message_id:
        await remember_private_screen(db, chat_id, int(message_id))
    return sent
