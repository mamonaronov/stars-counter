from __future__ import annotations

from dataclasses import replace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat, TelegramNetworkError
from aiogram.methods import GetChat
from aiogram.types import FSInputFile

from config import Config
from database.queries import Repo
from services.chat_migration import (
    MIGRATIONS_KEY,
    adopt_if_migrated,
    note_group_migration,
    remember_chat_migration,
    resolve_chat_id,
)
from services.group_export import KEY_GROUP, markers_chat_id, publish_markers_export
from services.telegram_backup import (
    CHAT_ID_KEY,
    CHAT_TITLE_KEY,
    content_fingerprint,
    send_telegram_backup,
    set_telegram_backup_chat,
    telegram_backup_chat,
)

OLD = -5410509348
NEW = -1004349789304


class _Msg:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


def _upgraded(old_id: int, new_id: int) -> TelegramMigrateToChat:
    return TelegramMigrateToChat(
        method=GetChat(chat_id=old_id),
        message="Bad Request: group chat was upgraded to a supergroup chat",
        migrate_to_chat_id=new_id,
    )


class _ExportBot:
    def __init__(self, *, migrate_once: bool = True, always: bool = False) -> None:
        self.docs: list[int] = []
        self.pinned: list[int] = []
        self.deleted: list[tuple[int, int]] = []
        self._n = 0
        self._migrate_once = migrate_once
        self._always = always
        self._migrated = False

    async def send_document(self, chat_id, document, caption=None, disable_notification=None):
        self.docs.append(chat_id)
        if self._always or (self._migrate_once and chat_id == OLD and not self._migrated):
            self._migrated = True
            successor = NEW if chat_id == OLD else -100999
            raise _upgraded(chat_id, successor)
        self._n += 1
        return _Msg(self._n)

    async def pin_chat_message(self, chat_id, message_id, disable_notification=None):
        self.pinned.append(chat_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _BackupBot:
    def __init__(self) -> None:
        self.docs: list[int] = []
        self._migrated = False

    async def send_document(self, chat_id, document, caption=None, disable_notification=None, request_timeout=None):
        assert isinstance(document, FSInputFile)
        self.docs.append(chat_id)
        if chat_id == OLD and not self._migrated:
            self._migrated = True
            raise _upgraded(chat_id, NEW)
        return _Msg(len(self.docs))


class _ChatBot:
    def __init__(self, exc: BaseException | None) -> None:
        self.exc = exc
        self.asked: list[int] = []

    async def get_chat(self, chat_id):
        self.asked.append(chat_id)
        if self.exc is not None:
            raise self.exc
        return {"id": chat_id}


async def _lesson(repo: Repo):
    student = await repo.create_student("Антонов Антон", 2024, "2026-09-01", 1)
    event = await repo.create_event("Лекция", "mandatory", "2026-09-01", "10:00", None, 1)
    return student, event


async def test_markers_export_follows_upgraded_group(repo: Repo, config: Config):
    await repo.db.set_system(KEY_GROUP, str(OLD))
    student, event = await _lesson(repo)
    bot = _ExportBot()
    cfg = replace(config, markers_group_id=None)

    assert await publish_markers_export(bot, repo, cfg, silent=True) == "sent"
    assert bot.docs == [OLD, NEW]
    assert bot.pinned == [NEW]
    assert await repo.db.get_system(KEY_GROUP) == str(NEW)
    assert await markers_chat_id(repo.db, cfg) == NEW

    bot._migrate_once = False
    await repo.set_presence(event.id, student.id, True, 1)
    assert await publish_markers_export(bot, repo, cfg, silent=True) == "sent"
    assert bot.docs[-1] == NEW
    assert OLD not in bot.docs[2:]


async def test_env_group_id_keeps_working_after_upgrade(repo: Repo, config: Config):
    student, event = await _lesson(repo)
    cfg = replace(config, markers_group_id=OLD)
    bot = _ExportBot()

    assert await publish_markers_export(bot, repo, cfg, silent=True) == "sent"
    assert await repo.db.get_system(KEY_GROUP) in (None, "")
    assert await markers_chat_id(repo.db, cfg) == NEW
    assert bot.docs == [OLD, NEW]

    bot._migrate_once = False
    await repo.db.set_system(KEY_GROUP, "")
    await repo.set_presence(event.id, student.id, True, 1)
    assert await publish_markers_export(bot, repo, cfg, silent=True, force=True) == "sent"
    assert bot.docs[-1] == NEW


async def test_upgrade_retries_once(repo: Repo, config: Config):
    await repo.db.set_system(KEY_GROUP, str(OLD))
    await _lesson(repo)
    bot = _ExportBot(always=True)

    with pytest.raises(TelegramMigrateToChat):
        await publish_markers_export(bot, repo, replace(config, markers_group_id=None), silent=True)

    assert bot.docs == [OLD, NEW]
    assert await repo.db.get_system(KEY_GROUP) == str(NEW)


async def test_service_message_rewrites_the_saved_group(repo: Repo, config: Config):
    await repo.db.set_system(KEY_GROUP, str(OLD))
    adopted = await note_group_migration(
        repo.db,
        chat_id=OLD,
        migrate_to_chat_id=NEW,
        migrate_from_chat_id=None,
    )
    assert adopted == NEW
    assert await markers_chat_id(repo.db, config) == NEW

    await repo.db.set_system(KEY_GROUP, str(OLD))
    adopted = await note_group_migration(
        repo.db,
        chat_id=NEW,
        migrate_to_chat_id=None,
        migrate_from_chat_id=OLD,
    )
    assert adopted == NEW
    assert await repo.db.get_system(KEY_GROUP) == str(NEW)


async def test_backup_send_follows_upgraded_group(repo: Repo, config: Config, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / ".env.example").write_text("BOT_TOKEN=\n", encoding="utf-8")
    cfg = replace(config, telegram_backup_root=root)
    await set_telegram_backup_chat(repo.db, OLD, "Отмечающие")
    await repo.db.set_system(KEY_GROUP, "-200")
    bot = _BackupBot()

    sent = await send_telegram_backup(repo.db, bot, cfg, silent=True)
    assert sent is not None
    assert bot.docs == [OLD, NEW]
    chat_id, title = await telegram_backup_chat(repo.db)
    assert chat_id == NEW
    assert title == "Отмечающие"
    assert await repo.db.get_system(CHAT_ID_KEY) == str(NEW)
    assert await repo.db.get_system(KEY_GROUP) == "-200"


async def test_migration_map_is_not_attendance_data(repo: Repo):
    before = await content_fingerprint(repo.db)
    await remember_chat_migration(repo.db, OLD, NEW)
    assert await repo.db.get_system(MIGRATIONS_KEY)
    assert await content_fingerprint(repo.db) == before
    assert await resolve_chat_id(repo.db, OLD) == NEW


async def test_migration_chain_collapses_to_the_latest_supergroup(repo: Repo):
    middle = -100111
    await repo.db.set_system(CHAT_ID_KEY, str(OLD))
    await repo.db.set_system(CHAT_TITLE_KEY, "Архив")
    await remember_chat_migration(repo.db, OLD, middle)
    await remember_chat_migration(repo.db, middle, NEW)
    assert await resolve_chat_id(repo.db, OLD) == NEW
    assert await repo.db.get_system(CHAT_ID_KEY) == str(NEW)
    assert await repo.db.get_system(CHAT_TITLE_KEY) == "Архив"


async def test_leave_during_upgrade_keeps_the_backup_binding(repo: Repo):
    await set_telegram_backup_chat(repo.db, OLD, "Отмечающие")
    kept = await adopt_if_migrated(_ChatBot(_upgraded(OLD, NEW)), repo.db, OLD)
    assert kept is True
    chat_id, title = await telegram_backup_chat(repo.db)
    assert chat_id == NEW
    assert title == "Отмечающие"


async def test_kick_is_not_treated_as_an_upgrade(repo: Repo):
    await set_telegram_backup_chat(repo.db, OLD, "Отмечающие")
    exc = TelegramBadRequest(method=GetChat(chat_id=OLD), message="Forbidden: bot was kicked from the group chat")
    assert await adopt_if_migrated(_ChatBot(exc), repo.db, OLD) is False
    assert await repo.db.get_system(CHAT_ID_KEY) == str(OLD)


async def test_telegram_outage_does_not_drop_the_binding(repo: Repo):
    exc = TelegramNetworkError(method=GetChat(chat_id=OLD), message="timeout")
    assert await adopt_if_migrated(_ChatBot(exc), repo.db, OLD) is True
