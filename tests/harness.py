"""Drive the real Dispatcher with a fake Telegram session."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import BaseStorage
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from config import Config
from database.database import Database
from database.fsm_storage import SqliteStorage
from database.queries import Repo
from handlers import setup_routers
from middlewares import ActorMiddleware, CallbackIdempotencyMiddleware, ContextMiddleware, ErrorIsolationMiddleware


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._msg_id = 1000

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError

    def _message(self, chat_id: int, text: str | None, reply_markup=None, message_id: int | None = None) -> Message:
        if message_id is None:
            self._msg_id += 1
            message_id = self._msg_id
        return Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            text=text,
            reply_markup=reply_markup,
        )

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="Bot", username="attend_test_bot")
        if name == "SendMessage":
            return self._message(method.chat_id, method.text, method.reply_markup)
        if name == "EditMessageText":
            return self._message(method.chat_id, method.text, method.reply_markup, method.message_id)
        if name == "EditMessageReplyMarkup":
            return self._message(method.chat_id, None, method.reply_markup, method.message_id)
        if name == "SendDocument":
            return self._message(method.chat_id, method.caption)
        return True

    # --- helpers for assertions -------------------------------------------------

    def last(self, name: str) -> TelegramMethod[Any] | None:
        for call in reversed(self.calls):
            if type(call).__name__ == name:
                return call
        return None

    def last_screen(self) -> TelegramMethod[Any] | None:
        for call in reversed(self.calls):
            if type(call).__name__ in {"SendMessage", "EditMessageText"}:
                return call
        return None

    def screen_text(self) -> str:
        screen = self.last_screen()
        return screen.text if screen is not None else ""

    def buttons(self) -> dict[str, str]:
        """text -> callback_data of the last screen."""
        screen = self.last_screen()
        markup = getattr(screen, "reply_markup", None)
        if markup is None:
            return {}
        return {b.text: b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data}

    def messages_to(self, chat_id: int) -> list[str]:
        return [c.text for c in self.calls if type(c).__name__ == "SendMessage" and c.chat_id == chat_id]


class _DelegatingStorage(BaseStorage):
    """Module-level routers can be attached to one Dispatcher only; swap the DB per test instead."""

    def __init__(self) -> None:
        self.inner: BaseStorage | None = None

    async def set_state(self, key, state=None):
        return await self.inner.set_state(key, state)

    async def get_state(self, key):
        return await self.inner.get_state(key)

    async def set_data(self, key, data):
        return await self.inner.set_data(key, data)

    async def get_data(self, key):
        return await self.inner.get_data(key)

    async def close(self):
        return None


_storage = _DelegatingStorage()
_context = ContextMiddleware(None, None, None)  # type: ignore[arg-type]
_dp: Dispatcher | None = None


def shared_dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        _dp = Dispatcher(storage=_storage)
        _dp.update.outer_middleware(_context)
        _dp.update.outer_middleware(ErrorIsolationMiddleware())
        _dp.update.outer_middleware(ActorMiddleware())
        _dp.callback_query.outer_middleware(CallbackIdempotencyMiddleware())
        _dp.include_router(setup_routers())
    return _dp


class Harness:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.repo = Repo(db)
        self.session = FakeSession()
        self.bot = Bot(token="42:TEST", default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=self.session)
        self.dp = shared_dispatcher()
        _storage.inner = SqliteStorage(db)
        _context.repo = self.repo
        _context.config = config
        _context.bot = self.bot
        self._update_id = 0
        self._cb_id = 0
        self.screens: dict[int, Message] = {}

    def _user(self, user_id: int) -> User:
        return User(id=user_id, is_bot=False, first_name=f"U{user_id}", username=f"user{user_id}")

    async def _feed(self, **kwargs) -> None:
        self._update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, **kwargs))
        screen = self.session.last_screen()
        if screen is not None:
            self.screens[screen.chat_id] = self.session._message(
                screen.chat_id, screen.text, screen.reply_markup, getattr(screen, "message_id", None)
            )

    async def text(self, user_id: int, text: str) -> None:
        message = Message(
            message_id=self._update_id + 1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=self._user(user_id),
            text=text,
        )
        await self._feed(message=message)

    async def click(self, user_id: int, data: str) -> None:
        self._cb_id += 1
        screen = self.screens.get(user_id) or self.session._message(user_id, "screen")
        cb = CallbackQuery(
            id=str(self._cb_id),
            from_user=self._user(user_id),
            chat_instance="ci",
            data=data,
            message=screen,
        )
        await self._feed(callback_query=cb)

    async def press(self, user_id: int, label_part: str) -> str:
        """Click the button of the last screen whose text contains label_part."""
        for text, data in self.session.buttons().items():
            if label_part in text:
                await self.click(user_id, data)
                return data
        raise AssertionError(f"button {label_part!r} not found among {list(self.session.buttons())}")

    def last_text(self) -> str:
        return self.session.screen_text()

    def buttons(self) -> dict[str, str]:
        return self.session.buttons()
