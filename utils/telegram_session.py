"""Telegram HTTP session that does not stall the bot when SOCKS ignores aiohttp timeouts.

Polling and everything else use different HTTP clients. A hung getUpdates can
close only the polling client. A client is closed only after the requests that
already borrowed it have finished, so one failure cannot produce
"Connector is closed" on a neighbor.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

from aiohttp import ClientError, ClientSession
from aiohttp.hdrs import USER_AGENT
from aiohttp.http import SERVER_SOFTWARE
from aiogram import Bot
from aiogram.__meta__ import __version__
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType

from utils.timeouts import await_or_abandon

logger = logging.getLogger(__name__)

_REQUEST_SLACK = 2.0
_CLOSE_TIMEOUT = 2.0
_SSL_CLOSE_PAUSE = 0.25
_POOL_POLL = "poll"
_POOL_API = "api"
_POLLING_METHOD = "getUpdates"

# The client a request borrowed. create_session reads it instead of the shared slot,
# so two concurrent calls cannot hand each other a client that is about to be closed.
_pinned: ContextVar[ClientSession | None] = ContextVar("telegram_http_client", default=None)


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


def _pool_name(method: TelegramMethod[Any]) -> str:
    if getattr(method, "__api_method__", "") == _POLLING_METHOD:
        return _POOL_POLL
    return _POOL_API


@dataclass(eq=False)
class _Lease:
    client: ClientSession
    users: int = 0
    retired: bool = False
    closing: bool = False


class AbandonableAiohttpSession(AiohttpSession):
    """Drop a hung SOCKS client without closing one that another request is using."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pools: dict[str, _Lease | None] = {_POOL_POLL: None, _POOL_API: None}
        self._leases: set[_Lease] = set()
        self._lock = asyncio.Lock()

    def _open_client(self) -> ClientSession:
        # Same construction as aiogram's create_session. A new connector each time,
        # so the polling client and the API client do not share a closed connector.
        return ClientSession(
            connector=self._connector_type(**dict(self._connector_init)),
            headers={USER_AGENT: f"{SERVER_SOFTWARE} aiogram/{__version__}"},
        )

    async def _acquire(self, pool: str) -> _Lease:
        async with self._lock:
            live = self._pools[pool]
            reusable = (
                live is not None
                and not live.retired
                and not live.closing
                and not getattr(live.client, "closed", True)
            )
            if not reusable:
                if live is not None:
                    live.retired = True
                live = _Lease(client=self._open_client())
                self._pools[pool] = live
                self._leases.add(live)
            live.users += 1
            return live

    async def _retire(self, pool: str, live: _Lease) -> None:
        """Stop handing this client out. Close it only when nobody is still inside a request."""
        async with self._lock:
            already = live.retired
            others = max(0, live.users - 1)
            live.retired = True
            if self._pools.get(pool) is live:
                self._pools[pool] = None
            should_close = live.users <= 0 and not live.closing
            if should_close:
                live.closing = True
                self._leases.discard(live)
        if others and not already:
            logger.info("telegram %s session retired, %s request(s) still using it", pool, others)
        if should_close:
            await self._close_client(live.client)

    async def _release(self, pool: str, live: _Lease) -> None:
        async with self._lock:
            live.users = max(0, live.users - 1)
            should_close = live.retired and live.users <= 0 and not live.closing
            if should_close:
                live.closing = True
                self._leases.discard(live)
                if self._pools.get(pool) is live:
                    self._pools[pool] = None
        if should_close:
            await self._close_client(live.client)

    async def _close_client(self, client: ClientSession) -> None:
        if getattr(client, "closed", True):
            return
        try:
            await await_or_abandon(client.close(), _CLOSE_TIMEOUT, name="telegram.session.close")
        except TimeoutError:
            logger.warning("telegram session close hung, connector dropped")
        await asyncio.sleep(_SSL_CLOSE_PAUSE)

    async def create_session(self) -> ClientSession:
        client = _pinned.get()
        if client is None:
            raise RuntimeError("Telegram HTTP client is borrowed only for the duration of a request")
        return client

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: Optional[int] = None,
    ) -> TelegramType:
        pool = _pool_name(method)
        live = await self._acquire(pool)
        token = _pinned.set(live.client)
        try:
            return await self._request(bot, method, timeout, pool, live)
        finally:
            _pinned.reset(token)
            await self._release(pool, live)

    async def _request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: Optional[int],
        pool: str,
        live: _Lease,
    ) -> TelegramType:
        limit = float(self.timeout if timeout is None else timeout)
        name = f"telegram.{getattr(method, '__api_method__', type(method).__name__)}"
        try:
            if limit <= 0:
                return await super().make_request(bot, method, timeout)
            return await await_or_abandon(
                super().make_request(bot, method, timeout),
                limit + _REQUEST_SLACK,
                name=name,
            )
        except TimeoutError as exc:
            # Retire the client this call borrowed, not whichever client is current now.
            # A neighbor that already opened a fresh client stays untouched.
            await self._retire(pool, live)
            raise TelegramNetworkError(method=method, message="Request timeout error") from exc
        except TelegramNetworkError:
            await self._retire(pool, live)
            raise

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        live = await self._acquire(_POOL_API)
        token = _pinned.set(live.client)
        try:
            try:
                async for chunk in super().stream_content(
                    url,
                    headers=headers,
                    timeout=timeout,
                    chunk_size=chunk_size,
                    raise_for_status=raise_for_status,
                ):
                    yield chunk
            except (TimeoutError, TelegramNetworkError, ClientError):
                await self._retire(_POOL_API, live)
                raise
        finally:
            _pinned.reset(token)
            await self._release(_POOL_API, live)

    async def close(self) -> None:
        async with self._lock:
            doomed = [live for live in self._leases if not live.closing]
            for live in doomed:
                live.retired = True
                live.closing = True
            self._leases.clear()
            for name in self._pools:
                self._pools[name] = None
        for live in doomed:
            await self._close_client(live.client)
