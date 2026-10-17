"""Aiogram middlewares: context injection, actor resolution, error isolation, callback dedup."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from config import Config
from database.queries import Repo
from services.alerts import format_alert, notify_alert
from services.roles import Actor
from utils.telegram import is_stale_callback_error, is_transient_telegram_error

logger = logging.getLogger(__name__)

# Clicks still in a handler. A failed handler leaves the id out of the database,
# so Telegram can deliver the same click again.
_inflight_callbacks: set[str] = set()
_inflight_lock = asyncio.Lock()


class ContextMiddleware(BaseMiddleware):
    def __init__(self, repo: Repo, config: Config, bot, scheduler=None) -> None:
        self.repo = repo
        self.config = config
        self.bot = bot
        self.scheduler = scheduler

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["repo"] = self.repo
        data["config"] = self.config
        data["app_bot"] = self.bot
        data["scheduler"] = self.scheduler
        return await handler(event, data)


def _extract_user(event: TelegramObject):
    if isinstance(event, Update):
        for attr in ("message", "callback_query", "edited_message", "my_chat_member"):
            obj = getattr(event, attr, None)
            if obj is not None and getattr(obj, "from_user", None) is not None:
                return obj.from_user
        return None
    return getattr(event, "from_user", None)


async def build_actor(repo: Repo, config: Config, telegram_id: int) -> Actor:
    user = await repo.get_user(telegram_id)
    student = await repo.get_student_by_telegram(telegram_id)
    return Actor(
        telegram_id=telegram_id,
        user=user,
        student=student,
        is_superadmin=telegram_id == config.superadmin_id,
    )


class ActorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user") or _extract_user(event)
        repo: Repo | None = data.get("repo")
        config: Config | None = data.get("config")
        if tg_user is not None and repo is not None and config is not None and not tg_user.is_bot:
            try:
                await repo.touch_user(tg_user.id, tg_user.username, tg_user.first_name, tg_user.last_name)
            except Exception:
                logger.exception("Failed to touch user %s", tg_user.id)
            data["actor"] = await build_actor(repo, config, tg_user.id)
        else:
            data["actor"] = None
        return await handler(event, data)


class ErrorIsolationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:
            if is_transient_telegram_error(exc):
                logger.warning("Handler skipped after a Telegram network error: %s", exc)
            elif is_stale_callback_error(exc):
                logger.info("Callback query expired before it was answered: %s", exc)
                return None
            else:
                logger.exception("Handler error")
                bot = data.get("app_bot") or data.get("bot")
                config: Config | None = data.get("config")
                if bot is not None and config is not None:
                    await notify_alert(bot, config, format_alert("handler", "Необработанная ошибка хендлера", exc=exc))
            target = None
            if isinstance(event, Update):
                target = event.callback_query or event.message
            elif isinstance(event, (CallbackQuery, Message)):
                target = event
            try:
                if isinstance(target, CallbackQuery):
                    await target.answer("Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
                elif isinstance(target, Message):
                    await target.answer("Произошла ошибка. Попробуйте ещё раз.")
            except Exception:
                pass
            return None


class CallbackIdempotencyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        callback_id = None
        if isinstance(event, CallbackQuery) and event.id and event.from_user:
            repo: Repo = data["repo"]
            async with _inflight_lock:
                if event.id in _inflight_callbacks or await repo.callback_claimed(event.id):
                    await event.answer()
                    return None
                _inflight_callbacks.add(event.id)
                callback_id = event.id
        try:
            result = await handler(event, data)
            if callback_id is not None and isinstance(event, CallbackQuery) and event.from_user:
                try:
                    await data["repo"].claim_callback(callback_id, event.from_user.id)
                except Exception:
                    logger.exception("Failed to remember callback %s", callback_id)
            return result
        finally:
            if callback_id is not None:
                _inflight_callbacks.discard(callback_id)
