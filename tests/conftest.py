from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from config import Config
from database.database import Database
from database.models import Event, Excuse, Student
from database.queries import Repo
from services.roles import Actor


def make_config(tmp_path: Path) -> Config:
    return Config(
        bot_token="0:test",
        superadmin_id=1,
        timezone="Europe/Moscow",
        db_path=tmp_path / "database.sqlite3",
        backup_path=tmp_path / "backups",
        backup_interval_hours=6,
        backup_keep=3,
        telegram_backup_interval_minutes=0,
        log_level="WARNING",
        telegram_proxy_url=None,
    )


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(make_config(tmp_path))
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def repo(db: Database):
    return Repo(db)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


def student(
    sid: int = 1,
    name: str = "Иванов Иван Иванович",
    *,
    joined: str = "2026-09-01",
    left: str | None = None,
    role: str | None = None,
    telegram_id: int | None = None,
    status: str = "active",
) -> Student:
    return Student(
        id=sid,
        full_name=name,
        enrollment_year=2024,
        telegram_id=telegram_id,
        verified_at="2026-09-01T00:00:00+00:00" if telegram_id else None,
        role=role,
        joined_on=joined,
        left_on=left,
        status=status,
        created_by=1,
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
    )


def event(eid: int, day: str, *, kind: str = "mandatory", closes: bool = False, title: str = "Лекция", time: str | None = None) -> Event:
    return Event(
        id=eid,
        title=title,
        kind=kind,
        event_date=day,
        start_time=time,
        template_id=None,
        closes_month=1 if closes else 0,
        month_title=None,
        created_by=1,
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
        deleted_at=None,
    )


def excuse(student_id: int, date_from: str, date_to: str, *, kind: str = "sick", cancelled: bool = False) -> Excuse:
    return Excuse(
        id=1,
        student_id=student_id,
        kind=kind,
        date_from=date_from,
        date_to=date_to,
        comment=None,
        created_by=1,
        created_at="2026-09-01T00:00:00+00:00",
        cancelled_at="2026-09-02T00:00:00+00:00" if cancelled else None,
        cancelled_by=1 if cancelled else None,
    )


def actor(role: str | None = None, *, superadmin: bool = False, sid: int = 100, telegram_id: int = 500) -> Actor:
    return Actor(
        telegram_id=telegram_id,
        user=None,
        student=student(sid, "Отмечающий Тест", role=role, telegram_id=telegram_id),
        is_superadmin=superadmin,
    )


TODAY = date(2026, 10, 15)
