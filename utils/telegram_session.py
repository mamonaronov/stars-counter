"""Telegram HTTP session that does not stall the bot when SOCKS ignores aiohttp timeouts."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType

from utils.timeouts import await_or_abandon

logger = logging.getLogger(__name__)

_REQUEST_SLACK = 2.0
_CLOSE_TIMEOUT = 2.0
_SSL_CLOSE_PAUSE = 0.25


def normalize_socks_proxy_url(proxy_url: str | None) -> str | None:
    """Map curl-style socks5h:// to socks5://. aiohttp-socks already resolves DNS remotely."""
    if not proxy_url:
        return proxy_url
    url = proxy_url.strip()
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    if scheme.lower() == "socks5h":
        return f"socks5://{rest}"
    return url


def make_telegram_session(proxy_url: str | None, timeout: float) -> AiohttpSession:
    if proxy_url:
        return AbandonableAiohttpSession(proxy=normalize_socks_proxy_url(proxy_url), timeout=timeout)
    return AbandonableAiohttpSession(timeout=timeout)


class AbandonableAiohttpSession(AiohttpSession):
    """Drop hung SOCKS connects so polling can retry instead of blocking forever."""

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: Optional[int] = None,
    ) -> TelegramType:
        limit = float(self.timeout if timeout is None else timeout)
        name = f"telegram.{getattr(method, '__api_method__', type(method).__name__)}"
        if limit <= 0:
            return await super().make_request(bot, method, timeout)
        try:
            result = await await_or_abandon(
                super().make_request(bot, method, timeout),
                limit + _REQUEST_SLACK,
                name=name,
            )
        except TimeoutError as exc:
            await self.drop_connector()
            raise TelegramNetworkError(method=method, message="Request timeout error") from exc
        except TelegramNetworkError:
            await self.drop_connector()
            raise
        return result

    async def drop_connector(self) -> None:
        old = self._session
        self._session = None
        self._should_reset_connector = True
        if old is None or getattr(old, "closed", True):
            return
        try:
            await await_or_abandon(old.close(), _CLOSE_TIMEOUT, name="telegram.session.close")
            await asyncio.sleep(_SSL_CLOSE_PAUSE)
        except TimeoutError:
            logger.warning("telegram session close hung, connector dropped")

    async def close(self) -> None:
        await self.drop_connector()
