"""Process stop requested from handlers or signals."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

_current: RuntimeControl | None = None


def get_runtime() -> RuntimeControl | None:
    return _current


def set_runtime(runtime: RuntimeControl | None) -> None:
    global _current
    _current = runtime


class RuntimeControl:
    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self._kick: Callable[[], None] | None = None

    def bind(self, kick: Callable[[], None]) -> None:
        self._kick = kick

    def request_stop(self) -> None:
        self.stop.set()
        if self._kick is not None:
            self._kick()
