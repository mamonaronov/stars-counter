"""Publish an Excel snapshot of attendance into the markers' Telegram group.

The file stays in the chat, so markers can download it when the bot is down.
A newer file replaces the previous one and is pinned.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Literal

from aiogram import Bot

from config import Config
from database.database import Database
from database.models import Event, Excuse, Student
from database.queries import Repo
from services.periods import build_periods, students_for_period
from services.report import PeriodSection, build_periods_xlsx
from services.stats import compute_group_stats
from utils.formatting import esc, events_word, plural_ru, students_word
from utils.telegram import bytes_file
from utils.time import local_now, local_today, now_utc, to_iso

logger = logging.getLogger(__name__)

KEY_GROUP = "markers_group_id"
KEY_FINGERPRINT = "markers_export_fingerprint"
KEY_SENT_AT = "markers_export_at"
KEY_MESSAGE_ID = "markers_export_message_id"
KEY_LAST_ERROR = "markers_export_last_error"

PublishResult = Literal["sent", "unchanged", "empty", "unconfigured"]

_lock = asyncio.Lock()
_pin_warned = False


@dataclass(frozen=True, slots=True)
class MarkersPayload:
    fingerprint: str
    data: bytes
    filename: str
    caption: str


@dataclass(frozen=True, slots=True)
class MarkersExport:
    fingerprint: str
    payload: MarkersPayload | None


async def markers_chat_id(db: Database, config: Config) -> int | None:
    """Saved /bindgroup chat wins over MARKERS_GROUP_ID."""
    raw = await db.get_system(KEY_GROUP)
    if raw and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            logger.warning("Ignoring invalid markers_group_id in system_info: %s", raw)
    return config.markers_group_id


def attendance_fingerprint(
    students: list[Student],
    events: list[Event],
    attendance: dict[int, set[int]],
    excuses: list[Excuse],
    *,
    org_title: str,
) -> str:
    lines = [f"org|{org_title}"]
    for student in sorted(students, key=lambda item: item.id):
        lines.append(
            "|".join(
                (
                    "s",
                    str(student.id),
                    student.full_name,
                    student.joined_on,
                    student.left_on or "",
                    student.status,
                )
            )
        )
    for event in events:
        lines.append(
            "|".join(
                (
                    "e",
                    str(event.id),
                    event.title,
                    event.kind,
                    event.event_date,
                    event.start_time or "",
                    str(event.closes_month),
                    event.month_title or "",
                )
            )
        )
    for event_id in sorted(attendance):
        ids = ",".join(str(student_id) for student_id in sorted(attendance[event_id]))
        lines.append(f"a|{event_id}|{ids}")
    for excuse in sorted(excuses, key=lambda item: item.id):
        lines.append(
            "|".join(
                (
                    "x",
                    str(excuse.id),
                    str(excuse.student_id),
                    excuse.kind,
                    excuse.date_from,
                    excuse.date_to,
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


async def _snapshot(repo: Repo, config: Config) -> tuple[str, list[PeriodSection] | None]:
    students = await repo.list_students(active_only=False)
    events = await repo.list_events()
    attendance = await repo.attendance_all()
    excuses = await repo.list_excuses(active_only=True)
    fingerprint = attendance_fingerprint(students, events, attendance, excuses, org_title=config.org_title)
    periods = [period for period in build_periods(events, local_today(config.timezone)) if period.events]
    if not periods:
        return fingerprint, None
    sections: list[PeriodSection] = []
    for period in periods:
        stats = compute_group_stats(
            students_for_period(students, period),
            period.events,
            attendance,
            excuses,
        )
        sections.append(
            PeriodSection(
                period_title=period.title,
                period_from=period.start_date,
                period_to=period.end_date,
                stats=stats,
                events=period.events,
                interim=not period.closed,
            )
        )
    return fingerprint, sections


def _payload(config: Config, fingerprint: str, sections: list[PeriodSection]) -> MarkersPayload:
    generated = local_now(config.timezone)
    student_count = len({item.student.id for section in sections for item in section.stats})
    event_count = sum(len(section.events) for section in sections)
    caption = (
        f"📊 <b>Посещаемость</b> · {esc(generated.strftime('%d.%m.%Y %H:%M'))}\n"
        f"{_count(len(sections), 'период', 'периода', 'периодов')} · "
        f"{event_count} {events_word(event_count)} · "
        f"{student_count} {students_word(student_count)}\n"
        "Копия для отмечающих: файл остаётся в группе, если бот недоступен."
    )
    return MarkersPayload(
        fingerprint=fingerprint,
        data=build_periods_xlsx(org_title=config.org_title, sections=sections),
        filename=f"Посещаемость_{generated.strftime('%Y-%m-%d_%H-%M')}.xlsx",
        caption=caption,
    )


async def prepare_markers_export(repo: Repo, config: Config) -> MarkersExport:
    fingerprint, sections = await _snapshot(repo, config)
    if sections is None:
        return MarkersExport(fingerprint=fingerprint, payload=None)
    return MarkersExport(fingerprint=fingerprint, payload=_payload(config, fingerprint, sections))


def _count(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {plural_ru(n, one, few, many)}"


async def publish_markers_export(bot: Bot, repo: Repo, config: Config, *, silent: bool, force: bool = False) -> PublishResult:
    async with _lock:
        chat_id = await markers_chat_id(repo.db, config)
        if chat_id is None:
            return "unconfigured"
        fingerprint, sections = await _snapshot(repo, config)
        if not force:
            last = await repo.db.get_system(KEY_FINGERPRINT)
            if last == fingerprint:
                return "unchanged"
        if sections is None:
            await repo.db.set_system(KEY_FINGERPRINT, fingerprint)
            return "empty"
        await _deliver(bot, repo.db, chat_id, _payload(config, fingerprint, sections), silent=silent)
        await repo.db.set_system(KEY_FINGERPRINT, fingerprint)
        await repo.db.set_system(KEY_SENT_AT, to_iso(now_utc()))
        if await repo.db.get_system(KEY_LAST_ERROR):
            await repo.db.set_system(KEY_LAST_ERROR, "")
        return "sent"


async def _deliver(bot: Bot, db: Database, chat_id: int, payload: MarkersPayload, *, silent: bool) -> None:
    previous = await db.get_system(KEY_MESSAGE_ID)
    message = await bot.send_document(
        chat_id,
        bytes_file(payload.data, payload.filename),
        caption=payload.caption,
        disable_notification=silent,
    )
    await db.set_system(KEY_MESSAGE_ID, str(message.message_id))
    await _pin(bot, chat_id, message.message_id)
    if previous and previous != str(message.message_id):
        try:
            await bot.delete_message(chat_id, int(previous))
        except Exception:
            logger.info("Could not delete previous attendance table %s", previous)


async def _pin(bot: Bot, chat_id: int, message_id: int) -> None:
    global _pin_warned
    try:
        await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
    except Exception as exc:
        if not _pin_warned:
            _pin_warned = True
            logger.warning(
                "Cannot pin attendance table in %s (%s). Make the bot a group admin with permission to pin messages.",
                chat_id,
                exc,
            )
