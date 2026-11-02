from __future__ import annotations

import asyncio

import pytest

from utils.timeouts import await_or_abandon


async def test_external_cancel_retrieves_child_exception():
    loop = asyncio.get_running_loop()
    captured: list[dict] = []

    def handler(_loop, context):
        captured.append(context)

    started = asyncio.Event()
    failed = asyncio.Event()

    async def child():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            failed.set()
            raise RuntimeError("probe failed after cancel")

    async def parent():
        await await_or_abandon(child(), 5, name="bot.get_me")

    loop.set_exception_handler(handler)
    outer = asyncio.create_task(parent())
    try:
        await started.wait()
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.wait_for(failed.wait(), timeout=1)
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(None)
    leaked = [item for item in captured if "never retrieved" in str(item.get("message", ""))]
    assert leaked == []
