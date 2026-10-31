from __future__ import annotations

import tarfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO

from aiogram.types import FSInputFile

from config import Config, load_config
from database.queries import Repo
from services.telegram_backup import (
    CHAT_ID_KEY,
    content_fingerprint,
    send_telegram_backup,
    telegram_backup_due,
)


class _Msg:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self._n = 0

    async def send_document(self, chat_id, document, caption=None, disable_notification=None, request_timeout=None):
        assert isinstance(document, FSInputFile)
        payload = document.path.read_bytes()
        self._n += 1
        self.docs.append({"chat_id": chat_id, "caption": caption, "silent": disable_notification, "name": document.filename, "payload": payload})
        return _Msg(self._n)


def test_backup_is_due_once_per_interval():
    start = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    assert telegram_backup_due(None, 60, start) is True
    assert telegram_backup_due(start, 60, start + timedelta(minutes=59)) is False
    assert telegram_backup_due(start, 60, start + timedelta(minutes=60)) is True
    assert telegram_backup_due(start, 0, start + timedelta(hours=5)) is False


def test_default_backup_interval_is_one_hour(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:test")
    monkeypatch.setenv("SUPERADMIN_TELEGRAM_ID", "1")
    monkeypatch.setattr("config.load_dotenv", lambda: None)
    monkeypatch.delenv("TELEGRAM_BACKUP_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("TELEGRAM_BACKUP_INTERVAL_HOURS", raising=False)
    assert load_config().telegram_backup_interval_minutes == 60
    monkeypatch.setenv("TELEGRAM_BACKUP_INTERVAL_HOURS", "2")
    assert load_config().telegram_backup_interval_minutes == 120
    monkeypatch.setenv("TELEGRAM_BACKUP_INTERVAL_MINUTES", "15")
    assert load_config().telegram_backup_interval_minutes == 15


async def test_group_backup_skips_until_data_changes(repo: Repo, config: Config, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / ".env.example").write_text("BOT_TOKEN=\n", encoding="utf-8")
    cfg = replace(config, telegram_backup_root=root)
    await repo.db.set_system(CHAT_ID_KEY, "-100777")
    bot = FakeBot()

    before = await content_fingerprint(repo.db)
    await repo.touch_user(7, "ann", "Анна", None)
    opened = await content_fingerprint(repo.db)
    assert opened != before
    await repo.touch_user(7, "ann", "Анна", None)
    assert await content_fingerprint(repo.db) == opened

    sent = await send_telegram_backup(repo.db, bot, cfg, silent=True)
    assert sent is not None
    assert await send_telegram_backup(repo.db, bot, cfg, silent=True) is None
    assert len(bot.docs) == 1
    assert bot.docs[0]["chat_id"] == -100777
    assert bot.docs[0]["silent"] is True
    assert bot.docs[0]["name"].endswith(".tgz")
    with tarfile.open(fileobj=BytesIO(bot.docs[0]["payload"]), mode="r:gz") as archive:
        names = archive.getnames()
    assert "database.sqlite3" in names
    assert ".env" in names

    await repo.create_student("Антонов Антон", 2024, "2026-09-01", 1)
    again = await send_telegram_backup(repo.db, bot, cfg, silent=True)
    assert again is not None
    assert len(bot.docs) == 2

    await send_telegram_backup(repo.db, bot, cfg, silent=False, chat_id=5, force=True)
    assert len(bot.docs) == 3
    assert bot.docs[-1]["chat_id"] == 5
    assert await send_telegram_backup(repo.db, bot, cfg, silent=True) is None
