from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.storage.base import StorageKey

from database.fsm_storage import SqliteStorage
from database.queries import Repo
from services.screen_memory import (
    SCREEN_COMMIT_KEY,
    attach_screen_memory,
    bind_screen_memory,
    forget_private_screen,
    private_screens,
    refresh_private_screens,
    remember_private_screen,
)
from services.telegram_backup import content_fingerprint
from tests.harness import FakeSession


def _bot(session: FakeSession) -> Bot:
    return Bot(token="0:test", default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)


def _commit(name: str):
    return lambda: (name, "title")


async def test_only_private_screens_with_buttons_are_remembered(repo: Repo):
    session = FakeSession()
    attach_screen_memory(session)
    bind_screen_memory(repo.db)
    bot = _bot(session)
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меню", callback_data="n:m")]])
        await bot.send_message(-100777, "группа", reply_markup=markup)
        await bot.send_message(7, "просто текст")
        sent = await bot.send_message(7, "экран", reply_markup=markup)
        await bot.edit_message_text("другой экран", chat_id=7, message_id=sent.message_id, reply_markup=markup)
    finally:
        bind_screen_memory(None)
        await bot.session.close()

    assert await private_screens(repo.db) == [(7, sent.message_id)]


async def test_screen_bookkeeping_does_not_change_the_backup_fingerprint(repo: Repo):
    before = await content_fingerprint(repo.db)
    await remember_private_screen(repo.db, 7, 15)
    await repo.db.set_system(SCREEN_COMMIT_KEY, "abc")
    assert await content_fingerprint(repo.db) == before
    await forget_private_screen(repo.db, 7)


async def test_new_commit_replaces_the_screen_and_drops_the_dialog(repo, config, monkeypatch):
    monkeypatch.setattr("services.screen_memory.app_build_identity", _commit("aaa"))
    await remember_private_screen(repo.db, 7, 15)
    storage = SqliteStorage(repo.db)
    key = StorageKey(bot_id=0, chat_id=7, user_id=7)
    await storage.set_state(key, "AddStudent:name")
    await storage.set_data(key, {"full_name": "Черновик"})

    session = FakeSession()
    bot = _bot(session)
    try:
        refreshed = await refresh_private_screens(bot, repo, config, storage)
        assert refreshed == {7}
        edited = [call for call in session.calls if type(call).__name__ == "EditMessageText"]
        assert len(edited) == 1
        assert edited[0].chat_id == 7
        assert edited[0].message_id == 15
        assert "Посещаемость" in edited[0].text
        assert edited[0].reply_markup is not None
        assert await storage.get_state(key) is None
        assert await storage.get_data(key) == {}
        assert await repo.db.get_system(SCREEN_COMMIT_KEY) == "aaa"

        await refresh_private_screens(bot, repo, config, storage)
        assert len([call for call in session.calls if type(call).__name__ == "EditMessageText"]) == 1

        monkeypatch.setattr("services.screen_memory.app_build_identity", _commit("bbb"))
        await storage.set_state(key, "AddStudent:name")
        again = await refresh_private_screens(bot, repo, config, storage)
        assert again == {7}
        assert await storage.get_state(key) is None
        assert await repo.db.get_system(SCREEN_COMMIT_KEY) == "bbb"
    finally:
        await bot.session.close()


async def test_missing_screen_is_replaced_with_a_new_menu(repo, config, monkeypatch):
    monkeypatch.setattr("services.screen_memory.app_build_identity", _commit("ccc"))
    await remember_private_screen(repo.db, 7, 15)

    class Gone(FakeSession):
        async def make_request(self, bot, method, timeout=None):
            if type(method).__name__ == "EditMessageText":
                raise TelegramBadRequest(method=method, message="message to edit not found")
            return await super().make_request(bot, method, timeout)

    session = Gone()
    bot = _bot(session)
    try:
        refreshed = await refresh_private_screens(bot, repo, config, None)
    finally:
        await bot.session.close()

    assert refreshed == {7}
    sent = [call for call in session.calls if type(call).__name__ == "SendMessage"]
    assert len(sent) == 1
    assert sent[0].chat_id == 7
    assert "Посещаемость" in sent[0].text
    stored = await private_screens(repo.db)
    assert stored[0][0] == 7
    assert stored[0][1] != 15


async def test_network_failure_retries_on_the_next_start(repo, config, monkeypatch):
    monkeypatch.setattr("services.screen_memory.app_build_identity", _commit("ddd"))
    await remember_private_screen(repo.db, 7, 15)

    class Down(FakeSession):
        async def make_request(self, bot, method, timeout=None):
            if type(method).__name__ == "EditMessageText":
                raise TelegramNetworkError(method=method, message="proxy down")
            return await super().make_request(bot, method, timeout)

    down = _bot(Down())
    try:
        assert await refresh_private_screens(down, repo, config, None) == set()
    finally:
        await down.session.close()
    assert await repo.db.get_system(SCREEN_COMMIT_KEY) is None

    up = _bot(FakeSession())
    try:
        assert await refresh_private_screens(up, repo, config, None) == {7}
    finally:
        await up.session.close()
    assert await repo.db.get_system(SCREEN_COMMIT_KEY) == "ddd"
