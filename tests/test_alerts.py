from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from services.alerts import BOT_STARTED_TEXT, BOT_STOPPED_TEXT, lifecycle_recipient_ids, notify_lifecycle
from tests.harness import FakeSession


def _buttons(call) -> set[str]:
    markup = getattr(call, "reply_markup", None)
    if markup is None:
        return set()
    return {button.text for row in markup.inline_keyboard for button in row}


async def _link_admins(repo) -> None:
    chief = await repo.create_student("Главный Админ", 2023, "2026-09-01", 1)
    admin = await repo.create_student("Обычный Админ", 2023, "2026-09-01", 1)
    marker = await repo.create_student("Отмечающий Маркер", 2023, "2026-09-01", 1)
    unbound = await repo.create_student("Админ Без Телеграма", 2023, "2026-09-01", 1)
    await repo.bind_telegram(chief.id, 41)
    await repo.bind_telegram(admin.id, 42)
    await repo.bind_telegram(marker.id, 43)
    await repo.set_role(chief.id, "chief_admin")
    await repo.set_role(admin.id, "admin")
    await repo.set_role(marker.id, "marker")
    await repo.set_role(unbound.id, "admin")


async def test_lifecycle_notice_reaches_admins_then_opens_their_menu(repo, config):
    await _link_admins(repo)
    assert await lifecycle_recipient_ids(repo, config) == [1, 41, 42]

    session = FakeSession()
    bot = Bot(token="0:test", default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)
    try:
        await notify_lifecycle(bot, repo, config, BOT_STARTED_TEXT)
    finally:
        await bot.session.close()

    sent = [call for call in session.calls if type(call).__name__ == "SendMessage"]
    by_chat: dict[int, list] = {}
    for call in sent:
        by_chat.setdefault(call.chat_id, []).append(call)

    assert set(by_chat) == {1, 41, 42}
    for chat_id in (1, 41, 42):
        notice, menu = by_chat[chat_id]
        assert notice.text == BOT_STARTED_TEXT
        assert notice.disable_notification is True
        assert notice.reply_markup is None
        assert "Посещаемость" in menu.text
        assert "Выберите действие:" in menu.text
        assert menu.disable_notification is False

    assert "🛠 Сервис" in _buttons(by_chat[1][1])
    assert "🧹 Очистить все данные" in _buttons(by_chat[41][1])
    assert "🛠 Сервис" not in _buttons(by_chat[41][1])
    assert "📈 Статистика бота" in _buttons(by_chat[42][1])
    assert "🧹 Очистить все данные" not in _buttons(by_chat[42][1])
    assert 43 not in by_chat


async def test_stop_notice_is_one_silent_line(repo, config):
    await _link_admins(repo)
    session = FakeSession()
    bot = Bot(token="0:test", default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)
    try:
        await notify_lifecycle(bot, repo, config, BOT_STOPPED_TEXT, with_menu=False)
    finally:
        await bot.session.close()

    sent = [call for call in session.calls if type(call).__name__ == "SendMessage"]
    by_chat: dict[int, list] = {}
    for call in sent:
        by_chat.setdefault(call.chat_id, []).append(call)

    assert set(by_chat) == {1, 41, 42}
    for chat_id in (1, 41, 42):
        assert len(by_chat[chat_id]) == 1
        notice = by_chat[chat_id][0]
        assert notice.text == BOT_STOPPED_TEXT
        assert notice.disable_notification is True
        assert notice.reply_markup is None
    assert 43 not in by_chat
