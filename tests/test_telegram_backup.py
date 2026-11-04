from __future__ import annotations

import sqlite3
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChat
from aiogram.types import FSInputFile

from config import Config, load_config
from database.queries import Repo
from services.jobs import telegram_backup_job
from services.telegram_backup import (
    CHAT_ID_KEY,
    FINGERPRINT_KEY,
    content_fingerprint,
    send_telegram_backup,
    telegram_backup_due,
    telegram_backup_retry_delay,
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

    async def send_message(self, *args, **kwargs):
        return _Msg(1)


def test_retry_delay_doubles_and_caps_at_one_hour():
    assert telegram_backup_retry_delay(0) == timedelta(minutes=1)
    assert telegram_backup_retry_delay(1) == timedelta(minutes=2)
    assert telegram_backup_retry_delay(2) == timedelta(minutes=4)
    assert telegram_backup_retry_delay(5) == timedelta(minutes=32)
    assert telegram_backup_retry_delay(6) == timedelta(hours=1)
    assert telegram_backup_retry_delay(12) == timedelta(hours=1)
    assert telegram_backup_retry_delay(-3) == timedelta(minutes=1)


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
    assert bot.docs[0]["name"].endswith(".zip")
    with zipfile.ZipFile(BytesIO(bot.docs[0]["payload"])) as archive:
        names = archive.namelist()
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


def _sqlite_from_archive(payload: bytes, dest) -> sqlite3.Connection:
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        dest.write_bytes(archive.read("database.sqlite3"))
    return sqlite3.connect(dest)


async def test_verification_and_other_edits_are_backed_up(repo: Repo, config: Config, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / ".env.example").write_text("BOT_TOKEN=\n", encoding="utf-8")
    cfg = replace(config, telegram_backup_root=root)
    await repo.db.set_system(CHAT_ID_KEY, "-100777")
    bot = FakeBot()

    student = await repo.create_student("Иванов Иван", 2024, "2026-09-01", 1)
    link = await repo.create_verification_link(student.id, 1)
    before = await content_fingerprint(repo.db)
    redeemed = await repo.redeem_verification(link.token, 42)
    assert redeemed.code == "ok"
    verified = await content_fingerprint(repo.db)
    assert verified != before

    async with repo.db.exclusive():
        await repo.db.conn.execute(
            "INSERT INTO fsm_storage (bot_id, chat_id, user_id, destiny, state, data, updated_at) VALUES (1, 1, 1, 'default', 'x', '{}', 't')"
        )
        await repo.db.conn.execute(
            "INSERT INTO callback_dedup (callback_id, telegram_id, created_at) VALUES ('cb', 1, 't')"
        )
        await repo.db.conn.execute(
            "UPDATE fsm_storage SET state = 'y', updated_at = 'later' WHERE user_id = 1"
        )
    assert await content_fingerprint(repo.db) == verified

    sent = await send_telegram_backup(repo.db, bot, cfg, silent=True)
    assert sent is not None
    with _sqlite_from_archive(bot.docs[0]["payload"], tmp_path / "from-backup.sqlite3") as restored:
        row = restored.execute(
            "SELECT telegram_id, verified_at FROM students WHERE id = ?",
            (student.id,),
        ).fetchone()
        used = restored.execute(
            "SELECT used_by FROM verification_links WHERE token = ?",
            (link.token,),
        ).fetchone()
    assert row[0] == 42
    assert row[1]
    assert used == (42,)
    assert await send_telegram_backup(repo.db, bot, cfg, silent=True) is None

    await repo.create_excuse(student.id, "sick", "2026-10-01", "2026-10-03", None, 1)
    excuse_backup = await send_telegram_backup(repo.db, bot, cfg, silent=True)
    assert excuse_backup is not None
    assert len(bot.docs) == 2


class _Scheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, trigger, **kwargs) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})


class _ProxyDownBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.alerts = 0

    async def send_document(self, chat_id, document, caption=None, disable_notification=None, request_timeout=None):
        raise TelegramNetworkError(method=GetChat(chat_id=chat_id), message="proxy down")

    async def send_message(self, *args, **kwargs):
        self.alerts += 1
        return _Msg(1)


async def test_proxy_outage_retries_with_backoff_and_keeps_the_change(repo: Repo, config: Config, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / ".env.example").write_text("BOT_TOKEN=\n", encoding="utf-8")
    cfg = replace(config, telegram_backup_root=root, telegram_backup_interval_minutes=60)
    await repo.db.set_system(CHAT_ID_KEY, "-100777")
    await repo.create_student("Петров Пётр", 2024, "2026-09-01", 1)
    down = _ProxyDownBot()
    scheduler = _Scheduler()

    started = datetime.now(timezone.utc)
    await telegram_backup_job(scheduler, repo.db, down, cfg)
    assert down.alerts == 1
    assert await repo.db.get_system(FINGERPRINT_KEY) is None
    assert list(cfg.backup_path.glob("stars-counter-backup_*.zip")) == []
    first = scheduler.jobs[-1]
    assert first["kwargs"]["retry_attempt"] == 1
    assert abs((first["run_date"] - started) - timedelta(minutes=1)) < timedelta(seconds=5)
    assert first["misfire_grace_time"] is None

    started = datetime.now(timezone.utc)
    await telegram_backup_job(scheduler, repo.db, down, cfg, retry_attempt=1)
    assert down.alerts == 1
    second = scheduler.jobs[-1]
    assert second["kwargs"]["retry_attempt"] == 2
    assert abs((second["run_date"] - started) - timedelta(minutes=2)) < timedelta(seconds=5)

    ok = FakeBot()
    started = datetime.now(timezone.utc)
    await telegram_backup_job(scheduler, repo.db, ok, cfg, retry_attempt=2)
    assert len(ok.docs) == 1
    assert await repo.db.get_system(FINGERPRINT_KEY)
    scheduled = scheduler.jobs[-1]
    assert scheduled["kwargs"]["retry_attempt"] == 0
    assert abs((scheduled["run_date"] - started) - timedelta(minutes=60)) < timedelta(seconds=5)
