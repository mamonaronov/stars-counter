from __future__ import annotations

import asyncio

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

from utils.telegram_session import AbandonableAiohttpSession


class _Call:
    def __init__(self, api: str, role: str = "") -> None:
        self.__api_method__ = api
        self.role = role


class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _install_clients(monkeypatch: pytest.MonkeyPatch) -> list[_Client]:
    clients: list[_Client] = []

    def open_client(self) -> _Client:
        client = _Client()
        clients.append(client)
        return client

    monkeypatch.setattr(AbandonableAiohttpSession, "_open_client", open_client)
    monkeypatch.setattr("utils.telegram_session._SSL_CLOSE_PAUSE", 0)
    monkeypatch.setattr("utils.telegram_session._REQUEST_SLACK", 0.05)
    return clients


def _session() -> AbandonableAiohttpSession:
    return AbandonableAiohttpSession(timeout=5)


async def test_polling_timeout_does_not_close_the_upload_client(monkeypatch: pytest.MonkeyPatch):
    clients = _install_clients(monkeypatch)
    poll_holding = asyncio.Event()
    release_upload = asyncio.Event()

    async def fake_request(self, bot, method, timeout=None):
        client = await self.create_session()
        if method.__api_method__ == "getUpdates":
            poll_holding.set()
            await asyncio.sleep(30)
            return client
        await poll_holding.wait()
        await release_upload.wait()
        if client.closed:
            raise TelegramNetworkError(method=method, message="ClientConnectionError: Connector is closed.")
        return client

    monkeypatch.setattr(AiohttpSession, "make_request", fake_request)
    session = _session()
    try:
        poll = asyncio.create_task(session.make_request(None, _Call("getUpdates"), timeout=0.2))
        await asyncio.wait_for(poll_holding.wait(), timeout=2)
        upload = asyncio.create_task(session.make_request(None, _Call("sendDocument"), timeout=5))
        with pytest.raises(TelegramNetworkError, match="Request timeout error"):
            await poll
        release_upload.set()
        uploaded = await upload
        assert clients[0] is not uploaded
        assert clients[0].closed is True
        assert uploaded.closed is False
    finally:
        release_upload.set()
        await session.close()


async def test_failed_call_retires_its_client_and_leaves_the_next_one_open(monkeypatch: pytest.MonkeyPatch):
    slow_in = asyncio.Event()
    release_slow = asyncio.Event()
    seen: dict[str, _Client] = {}

    async def fake_request(self, bot, method, timeout=None):
        client = await self.create_session()
        seen[method.role] = client
        if method.role == "slow":
            slow_in.set()
            await release_slow.wait()
            if client.closed:
                raise TelegramNetworkError(method=method, message="ClientConnectionError: Connector is closed.")
            return "kept"
        if method.role == "fail":
            await slow_in.wait()
            raise TelegramNetworkError(method=method, message="ClientOSError: reset")
        return client

    _install_clients(monkeypatch)
    monkeypatch.setattr(AiohttpSession, "make_request", fake_request)
    session = _session()
    try:
        slow = asyncio.create_task(session.make_request(None, _Call("sendDocument", "slow"), timeout=5))
        await asyncio.wait_for(slow_in.wait(), timeout=2)
        with pytest.raises(TelegramNetworkError, match="ClientOSError"):
            await session.make_request(None, _Call("sendMessage", "fail"), timeout=5)
        assert seen["slow"].closed is False
        nxt = await session.make_request(None, _Call("sendMessage", "next"), timeout=5)
        assert nxt is not seen["slow"]
        assert nxt.closed is False
        release_slow.set()
        assert await slow == "kept"
        assert seen["slow"].closed is True
        assert nxt.closed is False
    finally:
        release_slow.set()
        await session.close()
