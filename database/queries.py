"""Parameterized data-access layer."""

from __future__ import annotations

import json
import logging
import math
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Iterable

import aiosqlite

from database.database import Database
from database.models import (
    Attendance,
    AuditEntry,
    BotUsage,
    Event,
    EventTemplate,
    Excuse,
    Request,
    Student,
    User,
    VerificationLink,
    VpnLatencySample,
)
from utils.time import now_utc, to_iso

logger = logging.getLogger(__name__)

# A probe that got an answer is a ping. Mihomo lookup errors share this column
# and only mean the node name is unknown.
_VPN_OK_PING_SQL = "ok = 1 AND latency_ms IS NOT NULL"
_VPN_SAMPLE_COLS = "id, measured_at, ok, latency_ms, node_name, subscription, error, host_uptime_s"


def _latency_percentile(sorted_ms: list[int], p: float) -> int | None:
    if not sorted_ms:
        return None
    n = len(sorted_ms)
    idx = min(n - 1, max(0, math.ceil(p * n) - 1))
    return sorted_ms[idx]


def _count_ge(values: list[int], threshold: int | None) -> int:
    if threshold is None:
        return 0
    return sum(1 for value in values if value >= threshold)


def _apply_vpn_tail_stats(items: list[dict[str, Any]], grouped: dict[Any, list[int]], key) -> None:
    for item in items:
        values = sorted(grouped.get(key(item), []))
        p95_ms = _latency_percentile(values, 0.95)
        p99_ms = _latency_percentile(values, 0.99)
        p99_9_ms = _latency_percentile(values, 0.999)
        item["p95_ms"] = p95_ms
        item["p99_ms"] = p99_ms
        item["p99_9_ms"] = p99_9_ms
        item["p95_count"] = _count_ge(values, p95_ms)
        item["p99_count"] = _count_ge(values, p99_ms)
        item["p99_9_count"] = _count_ge(values, p99_9_ms)


def _empty_vpn_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "ok_count": 0,
        "fail_count": 0,
        "measured": 0,
        "avg_ms": None,
        "min_ms": None,
        "max_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "p99_9_ms": None,
        "p95_count": 0,
        "p99_count": 0,
        "p99_9_count": 0,
        "bucket_0_100": 0,
        "bucket_100_500": 0,
        "bucket_500_1000": 0,
        "bucket_1000": 0,
        "no_ping": 0,
    }


class RoleOccupiedError(RuntimeError):
    """The chief seat already belongs to another active student."""

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(role)


class RoleTransferError(RuntimeError):
    """The chief seat or the recipient changed before the transfer was stored."""


class MonthBoundaryError(RuntimeError):
    """The month closer changed before both updates were stored."""


class _TxAbort(Exception):
    def __init__(self, result: Any) -> None:
        self.result = result


@dataclass(frozen=True, slots=True)
class VerificationResult:
    code: str
    student: Student | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    code: str
    request: Request | None = None
    marked: bool = False


@dataclass(frozen=True, slots=True)
class WriteResult:
    last_id: int
    rowcount: int


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _opt(factory, row: aiosqlite.Row | None):
    return factory(**dict(row)) if row else None


def _many(factory, rows: Iterable[aiosqlite.Row]) -> list:
    return [factory(**dict(row)) for row in rows]


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def conn(self) -> aiosqlite.Connection:
        return self.db.conn

    @property
    def vpn_conn(self) -> aiosqlite.Connection:
        vpn_db = self.db.vpn_db
        if vpn_db is None:
            from database.database import DatabaseError

            raise DatabaseError("VPN database is not connected")
        return vpn_db.conn

    async def vpn_fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.vpn_conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def vpn_fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.vpn_conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchall()

    @asynccontextmanager
    async def transaction(self):
        async with self.db.exclusive():
            try:
                async with self.conn.execute("BEGIN IMMEDIATE"):
                    pass
                yield
                await self.conn.commit()
            except BaseException:
                await self.db._rollback()
                raise

    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.db.exclusive():
            return await self._fetchone_unlocked(sql, params)

    async def _fetchone_unlocked(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.db.exclusive():
            return await self._fetchall_unlocked(sql, params)

    async def _fetchall_unlocked(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        written = await self._write(sql, params)
        return written.last_id

    async def _write(self, sql: str, params: tuple = ()) -> WriteResult:
        async with self.db.exclusive():
            written = await self._write_unlocked(sql, params)
            await self.conn.commit()
            return written

    async def _write_unlocked(self, sql: str, params: tuple = ()) -> WriteResult:
        async with self.conn.execute(sql, params) as cur:
            return WriteResult(int(cur.lastrowid or 0), int(cur.rowcount))

    # ------------------------------------------------------------------ users

    async def touch_user(self, telegram_id: int, username: str | None, first_name: str | None, last_name: str | None) -> User:
        ts = to_iso(now_utc())
        await self._execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_seen_at = excluded.last_seen_at
            """,
            (telegram_id, username, first_name, last_name, ts, ts),
        )
        user = await self.get_user(telegram_id)
        assert user is not None
        return user

    async def get_user(self, telegram_id: int) -> User | None:
        return _opt(User, await self._fetchone("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)))

    async def bot_usage(self) -> BotUsage:
        """Accounts that wrote to the bot, and how many are linked to a student card.

        A removed student still counts as verified while the Telegram account stays bound.
        """
        row = await self._fetchone(
            """
            SELECT
                (SELECT COUNT(*) FROM users),
                (
                    SELECT COUNT(*) FROM users u
                    WHERE EXISTS (
                        SELECT 1 FROM students s WHERE s.telegram_id = u.telegram_id
                    )
                )
            """
        )
        if row is None:
            return BotUsage(0, 0)
        return BotUsage(int(row[0]), int(row[1]))

    # --------------------------------------------------------------- students

    async def create_student(
        self,
        full_name: str,
        enrollment_year: int | None,
        joined_on: str,
        created_by: int | None,
        *,
        direction: str | None = None,
        judaica_group: str | None = None,
        track: str | None = None,
    ) -> Student:
        ts = to_iso(now_utc())
        new_id = await self._execute(
            """
            INSERT INTO students (
                full_name, enrollment_year, direction, judaica_group, track, joined_on, status, created_by, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                full_name.strip(),
                enrollment_year,
                _blank(direction),
                _blank(judaica_group),
                _blank(track),
                joined_on,
                created_by,
                ts,
                ts,
            ),
        )
        student = await self.get_student(new_id)
        assert student is not None
        return student

    async def import_students(
        self,
        rows: list[tuple],
        joined_on: str,
        created_by: int | None,
    ) -> list[Student]:
        """Insert the whole list in one transaction. Names that already exist are skipped."""
        created_ids: list[int] = []
        async with self.transaction():
            for row in rows:
                full_name = row[0]
                enrollment_year = row[1]
                direction = row[2] if len(row) > 2 else None
                judaica_group = row[3] if len(row) > 3 else None
                track = row[4] if len(row) > 4 else None
                if await self._student_name_taken_unlocked(full_name):
                    continue
                ts = to_iso(now_utc())
                written = await self._write_unlocked(
                    """
                    INSERT INTO students (
                        full_name, enrollment_year, direction, judaica_group, track, joined_on, status, created_by, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        full_name.strip(),
                        enrollment_year,
                        _blank(direction),
                        _blank(judaica_group),
                        _blank(track),
                        joined_on,
                        created_by,
                        ts,
                        ts,
                    ),
                )
                created_ids.append(written.last_id)
        created: list[Student] = []
        for student_id in created_ids:
            student = await self.get_student(student_id)
            if student is not None:
                created.append(student)
        return created

    async def _student_name_taken_unlocked(self, full_name: str) -> bool:
        needle = " ".join(full_name.split()).casefold()
        rows = await self._fetchall_unlocked("SELECT full_name FROM students")
        return any(" ".join(str(row["full_name"]).split()).casefold() == needle for row in rows)

    async def get_student(self, student_id: int) -> Student | None:
        return _opt(Student, await self._fetchone("SELECT * FROM students WHERE id = ?", (student_id,)))

    async def get_student_by_telegram(self, telegram_id: int) -> Student | None:
        return _opt(
            Student,
            await self._fetchone("SELECT * FROM students WHERE telegram_id = ?", (telegram_id,)),
        )

    async def list_students(self, *, active_only: bool = True, query: str | None = None) -> list[Student]:
        """Sorted by name. Name filtering is done in Python: SQLite LOWER()/NOCASE are ASCII-only."""
        sql = "SELECT * FROM students"
        if active_only:
            sql += " WHERE status = 'active'"
        students = _many(Student, await self._fetchall(sql))
        needle = (query or "").casefold().strip()
        if needle:
            students = [s for s in students if needle in s.full_name.casefold()]
        students.sort(key=lambda s: (s.full_name.casefold(), s.id))
        return students

    async def list_students_with_roles(self) -> list[Student]:
        students = _many(
            Student,
            await self._fetchall("SELECT * FROM students WHERE role IS NOT NULL AND status = 'active'"),
        )
        students.sort(key=lambda s: (s.full_name.casefold(), s.id))
        return students

    async def list_verified_students(self) -> list[Student]:
        students = _many(
            Student,
            await self._fetchall("SELECT * FROM students WHERE telegram_id IS NOT NULL AND status = 'active'"),
        )
        students.sort(key=lambda s: (s.full_name.casefold(), s.id))
        return students

    async def find_student_by_name(self, full_name: str, *, active_only: bool = True) -> Student | None:
        needle = " ".join(full_name.split()).casefold()
        for student in await self.list_students(active_only=active_only):
            if " ".join(student.full_name.split()).casefold() == needle:
                return student
        return None

    async def update_student(self, student_id: int, **fields: Any) -> Student | None:
        allowed = {
            "full_name",
            "enrollment_year",
            "direction",
            "judaica_group",
            "track",
            "joined_on",
            "left_on",
            "status",
            "role",
            "telegram_id",
            "verified_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        for key in ("direction", "judaica_group", "track"):
            if key in updates:
                updates[key] = _blank(updates[key])
        if not updates:
            return await self.get_student(student_id)
        updates["updated_at"] = to_iso(now_utc())
        cols = ", ".join(f"{k} = ?" for k in updates)
        await self._execute(f"UPDATE students SET {cols} WHERE id = ?", (*updates.values(), student_id))
        return await self.get_student(student_id)

    async def remove_student(self, student_id: int, left_on: str) -> Student | None:
        return await self.update_student(student_id, status="removed", left_on=left_on, role=None)

    async def restore_student(self, student_id: int) -> Student | None:
        return await self.update_student(student_id, status="active", left_on=None)

    async def set_role(self, student_id: int, role: str | None) -> Student | None:
        try:
            return await self.update_student(student_id, role=role)
        except aiosqlite.IntegrityError as exc:
            if role and "UNIQUE" in str(exc).upper():
                raise RoleOccupiedError(role) from exc
            raise

    async def transfer_role(self, from_student_id: int, to_student_id: int, role: str, demoted_role: str) -> None:
        """Hand a chief seat over. Both updates commit together or not at all."""
        ts = to_iso(now_utc())
        async with self.transaction():
            demoted = await self._write_unlocked(
                """
                UPDATE students SET role = ?, updated_at = ?
                WHERE id = ? AND role = ? AND status = 'active'
                """,
                (demoted_role, ts, from_student_id, role),
            )
            if demoted.rowcount != 1:
                raise RoleTransferError()
            promoted = await self._write_unlocked(
                """
                UPDATE students SET role = ?, updated_at = ?
                WHERE id = ? AND status = 'active' AND telegram_id IS NOT NULL AND id != ?
                """,
                (role, ts, to_student_id, from_student_id),
            )
            if promoted.rowcount != 1:
                raise RoleTransferError()

    async def bind_telegram(self, student_id: int, telegram_id: int) -> Student | None:
        return await self.update_student(student_id, telegram_id=telegram_id, verified_at=to_iso(now_utc()))

    async def unbind_telegram(self, student_id: int) -> Student | None:
        return await self.update_student(student_id, telegram_id=None, verified_at=None, role=None)

    # ------------------------------------------------------------ verification

    async def create_verification_link(self, student_id: int, created_by: int) -> VerificationLink:
        ts = to_iso(now_utc())
        token = secrets.token_urlsafe(18)
        async with self.transaction():
            await self._write_unlocked(
                "UPDATE verification_links SET revoked_at = ? WHERE student_id = ? AND used_at IS NULL AND revoked_at IS NULL",
                (ts, student_id),
            )
            await self._write_unlocked(
                "INSERT INTO verification_links (token, student_id, created_by, created_at) VALUES (?, ?, ?, ?)",
                (token, student_id, created_by, ts),
            )
        link = await self.get_verification_link(token)
        assert link is not None
        return link

    async def get_verification_link(self, token: str) -> VerificationLink | None:
        return _opt(
            VerificationLink,
            await self._fetchone("SELECT * FROM verification_links WHERE token = ?", (token,)),
        )

    async def use_verification_link(self, token: str, used_by: int) -> bool:
        written = await self._write(
            """
            UPDATE verification_links SET used_at = ?, used_by = ?
            WHERE token = ? AND used_at IS NULL AND revoked_at IS NULL
            """,
            (to_iso(now_utc()), used_by, token),
        )
        return written.rowcount == 1

    async def redeem_verification(self, token: str, telegram_id: int) -> VerificationResult:
        """Bind the student and consume the link, or leave both unchanged."""
        try:
            async with self.transaction():
                student_id = await self._redeem_unlocked(token, telegram_id)
        except _TxAbort as exc:
            return exc.result
        student = await self.get_student(student_id)
        return VerificationResult("ok", student)

    async def _redeem_unlocked(self, token: str, telegram_id: int) -> int:
        link = _opt(VerificationLink, await self._fetchone_unlocked("SELECT * FROM verification_links WHERE token = ?", (token,)))
        if link is None:
            raise _TxAbort(VerificationResult("missing"))
        if link.revoked_at or link.used_at:
            raise _TxAbort(VerificationResult("used"))
        student = _opt(Student, await self._fetchone_unlocked("SELECT * FROM students WHERE id = ?", (link.student_id,)))
        if student is None or not student.is_active:
            raise _TxAbort(VerificationResult("inactive"))
        if student.telegram_id is not None and student.telegram_id != telegram_id:
            raise _TxAbort(VerificationResult("taken"))
        existing = _opt(Student, await self._fetchone_unlocked("SELECT * FROM students WHERE telegram_id = ?", (telegram_id,)))
        if existing is not None and existing.id != student.id:
            code = "linked_removed" if not existing.is_active else "linked_other"
            raise _TxAbort(VerificationResult(code, existing))
        ts = to_iso(now_utc())
        claimed = await self._write_unlocked(
            """
            UPDATE verification_links SET used_at = ?, used_by = ?
            WHERE token = ? AND used_at IS NULL AND revoked_at IS NULL
            """,
            (ts, telegram_id, token),
        )
        if claimed.rowcount != 1:
            raise _TxAbort(VerificationResult("used"))
        bound = await self._write_unlocked(
            """
            UPDATE students SET telegram_id = ?, verified_at = ?, updated_at = ?
            WHERE id = ? AND status = 'active' AND (telegram_id IS NULL OR telegram_id = ?)
            """,
            (telegram_id, ts, ts, student.id, telegram_id),
        )
        if bound.rowcount != 1:
            raise _TxAbort(VerificationResult("taken"))
        return student.id

    # -------------------------------------------------------------- templates

    async def create_template(self, title: str, kind: str, default_time: str | None, created_by: int) -> EventTemplate:
        new_id = await self._execute(
            "INSERT INTO event_templates (title, kind, default_time, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (title.strip(), kind, default_time, created_by, to_iso(now_utc())),
        )
        tpl = await self.get_template(new_id)
        assert tpl is not None
        return tpl

    async def get_template(self, template_id: int) -> EventTemplate | None:
        return _opt(EventTemplate, await self._fetchone("SELECT * FROM event_templates WHERE id = ?", (template_id,)))

    async def list_templates(self) -> list[EventTemplate]:
        templates = _many(
            EventTemplate,
            await self._fetchall("SELECT * FROM event_templates WHERE archived_at IS NULL"),
        )
        templates.sort(key=lambda item: (item.title.casefold(), item.id))
        return templates

    async def archive_template(self, template_id: int) -> None:
        await self._execute("UPDATE event_templates SET archived_at = ? WHERE id = ?", (to_iso(now_utc()), template_id))

    async def update_template(self, template_id: int, **fields: Any) -> EventTemplate | None:
        allowed = {"title", "kind", "default_time"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if updates:
            cols = ", ".join(f"{k} = ?" for k in updates)
            await self._execute(f"UPDATE event_templates SET {cols} WHERE id = ?", (*updates.values(), template_id))
        return await self.get_template(template_id)

    # ----------------------------------------------------------------- events

    async def create_event(
        self,
        title: str,
        kind: str,
        event_date: str,
        start_time: str | None,
        template_id: int | None,
        created_by: int,
    ) -> Event:
        ts = to_iso(now_utc())
        new_id = await self._execute(
            """
            INSERT INTO events (title, kind, event_date, start_time, template_id, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title.strip(), kind, event_date, start_time, template_id, created_by, ts, ts),
        )
        event = await self.get_event(new_id)
        assert event is not None
        return event

    async def get_event(self, event_id: int) -> Event | None:
        return _opt(Event, await self._fetchone("SELECT * FROM events WHERE id = ?", (event_id,)))

    async def list_events(self, *, include_deleted: bool = False) -> list[Event]:
        """All events in chronological order."""
        sql = "SELECT * FROM events"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY event_date, COALESCE(start_time, ''), id"
        return _many(Event, await self._fetchall(sql))

    async def update_event(self, event_id: int, **fields: Any) -> Event | None:
        allowed = {"title", "kind", "event_date", "start_time", "closes_month", "month_title", "deleted_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_event(event_id)
        updates["updated_at"] = to_iso(now_utc())
        cols = ", ".join(f"{k} = ?" for k in updates)
        await self._execute(f"UPDATE events SET {cols} WHERE id = ?", (*updates.values(), event_id))
        return await self.get_event(event_id)

    async def set_month_closer(self, event_id: int, *, previous_closer_id: int | None, month_title: str | None) -> None:
        """Move or set the month boundary. The old closer is cleared in the same transaction."""
        ts = to_iso(now_utc())
        async with self.transaction():
            if previous_closer_id is not None and previous_closer_id != event_id:
                cleared = await self._write_unlocked(
                    """
                    UPDATE events
                    SET closes_month = 0, month_title = NULL, updated_at = ?
                    WHERE id = ? AND deleted_at IS NULL AND closes_month = 1
                    """,
                    (ts, previous_closer_id),
                )
                if cleared.rowcount != 1:
                    raise MonthBoundaryError()
            closed = await self._write_unlocked(
                """
                UPDATE events
                SET closes_month = 1, month_title = ?, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL AND closes_month = 0
                """,
                (month_title, ts, event_id),
            )
            if closed.rowcount != 1:
                raise MonthBoundaryError()

    async def delete_event(self, event_id: int, *, successor_id: int | None = None, month_title: str | None = None) -> None:
        """Soft-delete an event. A successor, when given, becomes the closer in the same transaction."""
        ts = to_iso(now_utc())
        async with self.transaction():
            if successor_id is not None:
                shifted = await self._write_unlocked(
                    """
                    UPDATE events
                    SET closes_month = 1, month_title = ?, updated_at = ?
                    WHERE id = ? AND deleted_at IS NULL AND closes_month = 0
                    """,
                    (month_title, ts, successor_id),
                )
                if shifted.rowcount != 1:
                    raise MonthBoundaryError()
            deleted = await self._write_unlocked(
                """
                UPDATE events
                SET deleted_at = ?, closes_month = 0, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (ts, ts, event_id),
            )
            if deleted.rowcount != 1:
                raise MonthBoundaryError()

    # ------------------------------------------------------------- attendance

    async def attendance_for_event(self, event_id: int) -> dict[int, Attendance]:
        rows = await self._fetchall("SELECT * FROM attendance WHERE event_id = ?", (event_id,))
        return {int(row["student_id"]): Attendance(**dict(row)) for row in rows}

    async def attendance_student_ids(self, event_id: int) -> set[int]:
        rows = await self._fetchall("SELECT student_id FROM attendance WHERE event_id = ?", (event_id,))
        return {int(row[0]) for row in rows}

    async def attendance_counts(self, event_ids: Iterable[int]) -> dict[int, int]:
        ids = list(event_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = await self._fetchall(
            f"SELECT event_id, COUNT(*) AS n FROM attendance WHERE event_id IN ({placeholders}) GROUP BY event_id",
            tuple(ids),
        )
        return {int(row["event_id"]): int(row["n"]) for row in rows}

    async def attendance_all(self) -> dict[int, set[int]]:
        """event_id -> set of student ids. Small dataset (hundreds of rows per month)."""
        rows = await self._fetchall("SELECT event_id, student_id FROM attendance")
        result: dict[int, set[int]] = {}
        for row in rows:
            result.setdefault(int(row["event_id"]), set()).add(int(row["student_id"]))
        return result

    async def attendance_of_student(self, student_id: int) -> set[int]:
        rows = await self._fetchall("SELECT event_id FROM attendance WHERE student_id = ?", (student_id,))
        return {int(row[0]) for row in rows}

    async def is_present(self, event_id: int, student_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM attendance WHERE event_id = ? AND student_id = ?", (event_id, student_id)
        )
        return row is not None

    async def set_presence(self, event_id: int, student_id: int, present: bool, marked_by: int) -> bool:
        """Returns True if the state changed."""
        if present:
            written = await self._write(
                """
                INSERT INTO attendance (event_id, student_id, marked_by, marked_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id, student_id) DO NOTHING
                """,
                (event_id, student_id, marked_by, to_iso(now_utc())),
            )
        else:
            written = await self._write(
                "DELETE FROM attendance WHERE event_id = ? AND student_id = ?",
                (event_id, student_id),
            )
        return written.rowcount == 1

    async def toggle_presence(self, event_id: int, student_id: int, marked_by: int, *, allow_mark: bool) -> tuple[bool, bool]:
        """Flip presence in one transaction. Returns (changed, is_present)."""
        changed = False
        present = False
        async with self.transaction():
            deleted = await self._write_unlocked(
                "DELETE FROM attendance WHERE event_id = ? AND student_id = ?",
                (event_id, student_id),
            )
            if deleted.rowcount > 0:
                changed = True
                present = False
            elif allow_mark:
                inserted = await self._write_unlocked(
                    """
                    INSERT INTO attendance (event_id, student_id, marked_by, marked_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(event_id, student_id) DO NOTHING
                    """,
                    (event_id, student_id, marked_by, to_iso(now_utc())),
                )
                present = True
                changed = inserted.rowcount == 1
        return changed, present

    # ---------------------------------------------------------------- excuses

    async def create_excuse(
        self,
        student_id: int,
        kind: str,
        date_from: str,
        date_to: str,
        comment: str | None,
        created_by: int,
    ) -> Excuse:
        new_id = await self._execute(
            """
            INSERT INTO excuses (student_id, kind, date_from, date_to, comment, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (student_id, kind, date_from, date_to, comment, created_by, to_iso(now_utc())),
        )
        excuse = await self.get_excuse(new_id)
        assert excuse is not None
        return excuse

    async def get_excuse(self, excuse_id: int) -> Excuse | None:
        return _opt(Excuse, await self._fetchone("SELECT * FROM excuses WHERE id = ?", (excuse_id,)))

    async def list_excuses(self, *, active_only: bool = True, student_id: int | None = None) -> list[Excuse]:
        sql = "SELECT * FROM excuses"
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("cancelled_at IS NULL")
        if student_id is not None:
            clauses.append("student_id = ?")
            params.append(student_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY date_from DESC, id DESC"
        return _many(Excuse, await self._fetchall(sql, tuple(params)))

    async def cancel_excuse(self, excuse_id: int, cancelled_by: int) -> None:
        await self._execute(
            "UPDATE excuses SET cancelled_at = ?, cancelled_by = ? WHERE id = ? AND cancelled_at IS NULL",
            (to_iso(now_utc()), cancelled_by, excuse_id),
        )

    # --------------------------------------------------------------- requests

    async def create_request(self, student_id: int, event_id: int, text: str) -> Request | None:
        """None when this student already has an open request for the event."""
        try:
            new_id = await self._execute(
                "INSERT INTO requests (student_id, event_id, text, status, created_at) VALUES (?, ?, ?, 'open', ?)",
                (student_id, event_id, text.strip(), to_iso(now_utc())),
            )
        except aiosqlite.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                return None
            raise
        return await self.get_request(new_id)

    async def get_request(self, request_id: int) -> Request | None:
        return _opt(Request, await self._fetchone("SELECT * FROM requests WHERE id = ?", (request_id,)))

    async def list_requests(
        self,
        *,
        status: str | None = "open",
        closed_only: bool = False,
        student_id: int | None = None,
        limit: int = 200,
    ) -> list[Request]:
        sql = "SELECT * FROM requests"
        clauses: list[str] = []
        params: list[Any] = []
        if closed_only:
            clauses.append("status != 'open'")
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if student_id is not None:
            clauses.append("student_id = ?")
            params.append(student_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return _many(Request, await self._fetchall(sql, tuple(params)))

    async def count_open_requests(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) FROM requests WHERE status = 'open'")
        return int(row[0]) if row else 0

    async def has_open_request(self, student_id: int, event_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM requests WHERE student_id = ? AND event_id = ? AND status = 'open'",
            (student_id, event_id),
        )
        return row is not None

    async def resolve_request(self, request_id: int, status: str, resolved_by: int, comment: str | None) -> Request | None:
        """Close an open request. Returns None when another marker already resolved it."""
        written = await self._write(
            """
            UPDATE requests
            SET status = ?, resolved_by = ?, resolved_at = ?, resolution_comment = ?
            WHERE id = ? AND status = 'open'
            """,
            (status, resolved_by, to_iso(now_utc()), comment, request_id),
        )
        if written.rowcount != 1:
            return None
        return await self.get_request(request_id)

    async def approve_request_with_attendance(self, request_id: int, resolved_by: int) -> Approval:
        """Mark the student and close the request in one transaction."""
        try:
            async with self.transaction():
                marked = await self._approve_unlocked(request_id, resolved_by)
        except _TxAbort as exc:
            return exc.result
        return Approval("ok", await self.get_request(request_id), marked)

    async def _approve_unlocked(self, request_id: int, resolved_by: int) -> bool:
        req = _opt(Request, await self._fetchone_unlocked("SELECT * FROM requests WHERE id = ?", (request_id,)))
        if req is None or req.status != "open":
            raise _TxAbort(Approval("resolved"))
        student = _opt(Student, await self._fetchone_unlocked("SELECT * FROM students WHERE id = ?", (req.student_id,)))
        event = _opt(Event, await self._fetchone_unlocked("SELECT * FROM events WHERE id = ?", (req.event_id,)))
        if student is None or event is None or event.deleted_at:
            raise _TxAbort(Approval("missing"))
        if not student.counts_on(event.day):
            raise _TxAbort(Approval("not_counted"))
        ts = to_iso(now_utc())
        inserted = await self._write_unlocked(
            """
            INSERT INTO attendance (event_id, student_id, marked_by, marked_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, student_id) DO NOTHING
            """,
            (event.id, student.id, resolved_by, ts),
        )
        closed = await self._write_unlocked(
            """
            UPDATE requests
            SET status = 'approved', resolved_by = ?, resolved_at = ?, resolution_comment = NULL
            WHERE id = ? AND status = 'open'
            """,
            (resolved_by, ts, request_id),
        )
        if closed.rowcount != 1:
            raise _TxAbort(Approval("resolved"))
        return inserted.rowcount == 1

    # ------------------------------------------------------------------ audit

    async def add_audit(
        self,
        *,
        actor_telegram_id: int,
        actor_student_id: int | None,
        actor_name: str | None,
        actor_role: str,
        action: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        return await self._execute(
            """
            INSERT INTO audit_log (actor_telegram_id, actor_student_id, actor_name, actor_role, action,
                                   entity_type, entity_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_telegram_id,
                actor_student_id,
                actor_name,
                actor_role,
                action,
                entity_type,
                entity_id,
                json.dumps(details, ensure_ascii=False) if details else None,
                to_iso(now_utc()),
            ),
        )

    async def list_audit(self, *, limit: int = 20, offset: int = 0, entity_type: str | None = None, entity_id: int | None = None) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if entity_type is not None:
            sql += " WHERE entity_type = ?"
            params.append(entity_type)
            if entity_id is not None:
                sql += " AND entity_id = ?"
                params.append(entity_id)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return _many(AuditEntry, await self._fetchall(sql, tuple(params)))

    async def count_audit(self, *, entity_type: str | None = None, entity_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) FROM audit_log"
        params: list[Any] = []
        if entity_type is not None:
            sql += " WHERE entity_type = ?"
            params.append(entity_type)
            if entity_id is not None:
                sql += " AND entity_id = ?"
                params.append(entity_id)
        row = await self._fetchone(sql, tuple(params))
        return int(row[0]) if row else 0

    # --------------------------------------------------------------- callbacks

    async def callback_claimed(self, callback_id: str) -> bool:
        row = await self._fetchone("SELECT 1 FROM callback_dedup WHERE callback_id = ?", (callback_id,))
        return row is not None

    async def claim_callback(self, callback_id: str, telegram_id: int) -> bool:
        try:
            await self._execute(
                "INSERT INTO callback_dedup (callback_id, telegram_id, created_at) VALUES (?, ?, ?)",
                (callback_id, telegram_id, to_iso(now_utc())),
            )
            return True
        except aiosqlite.IntegrityError:
            return False

    async def cleanup_callbacks(self, before_iso: str) -> None:
        await self._execute("DELETE FROM callback_dedup WHERE created_at < ?", (before_iso,))

    # ------------------------------------------------------------------ stats

    async def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for name, sql in (
            ("students", "SELECT COUNT(*) FROM students WHERE status = 'active'"),
            ("verified", "SELECT COUNT(*) FROM students WHERE status = 'active' AND telegram_id IS NOT NULL"),
            ("events", "SELECT COUNT(*) FROM events WHERE deleted_at IS NULL"),
            ("attendance", "SELECT COUNT(*) FROM attendance"),
            ("audit", "SELECT COUNT(*) FROM audit_log"),
            ("users", "SELECT COUNT(*) FROM users"),
        ):
            row = await self._fetchone(sql)
            result[name] = int(row[0]) if row else 0
        return result

    # ----------------------------------------------------------- ping samples

    async def insert_vpn_sample(
        self,
        measured_at: str,
        ok: bool,
        latency_ms: int | None,
        node_name: str | None,
        subscription: str | None,
        error: str | None,
        host_uptime_s: float | None = None,
    ) -> int:
        vpn = self.db.vpn_db
        if vpn is None:
            from database.database import DatabaseError

            raise DatabaseError("VPN database is not connected")
        cur = await vpn.conn.execute(
            """
            INSERT INTO vpn_latency_samples
                (measured_at, ok, latency_ms, node_name, subscription, error, host_uptime_s)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (measured_at, 1 if ok else 0, latency_ms, node_name, subscription, error, host_uptime_s),
        )
        await self.db.vpn_db.conn.commit()
        return int(cur.lastrowid or 0)

    async def latest_vpn_sample(self) -> VpnLatencySample | None:
        vpn = self.db.vpn_db
        if vpn is None:
            return None
        async with vpn.conn.execute(
            """
            SELECT id, measured_at, ok, latency_ms, node_name, subscription, error, host_uptime_s
            FROM vpn_latency_samples
            ORDER BY measured_at DESC, id DESC
            LIMIT 1
            """
        ) as cur:
            row = await cur.fetchone()
        return VpnLatencySample(**dict(row)) if row else None

    async def count_vpn_samples(self) -> int:
        vpn = self.db.vpn_db
        if vpn is None:
            return 0
        async with vpn.conn.execute("SELECT COUNT(*) FROM vpn_latency_samples") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def service_started_at(self) -> str | None:
        row = await self._fetchone("SELECT MIN(first_seen_at) AS ts FROM users")
        return str(row["ts"]) if row and row["ts"] else None

    async def list_vpn_samples(self, start: str, end: str) -> list[VpnLatencySample]:
        if self.db.vpn_db is None:
            return []
        rows = await self.vpn_fetchall(
            f"""
            SELECT {_VPN_SAMPLE_COLS}
            FROM vpn_latency_samples
            WHERE measured_at >= ? AND measured_at < ?
            ORDER BY measured_at ASC, id ASC
            """,
            (start, end),
        )
        return [VpnLatencySample(**dict(row)) for row in rows]

    async def list_vpn_heartbeats(self, start: str, end: str) -> list[tuple[str, float | None]]:
        if self.db.vpn_db is None:
            return []
        rows = await self.vpn_fetchall(
            """
            SELECT measured_at, host_uptime_s
            FROM vpn_latency_samples
            WHERE measured_at >= ? AND measured_at < ?
            ORDER BY measured_at ASC, id ASC
            """,
            (start, end),
        )
        out: list[tuple[str, float | None]] = []
        for row in rows:
            raw = row["host_uptime_s"]
            host_up = float(raw) if raw is not None else None
            out.append((str(row["measured_at"]), host_up))
        return out

    async def earliest_vpn_measured_at(self) -> str | None:
        if self.db.vpn_db is None:
            return None
        row = await self.vpn_fetchone("SELECT MIN(measured_at) AS ts FROM vpn_latency_samples")
        return str(row["ts"]) if row and row["ts"] else None

    async def vpn_latency_summary(self, start: str, end: str) -> dict[str, Any]:
        if self.db.vpn_db is None:
            return _empty_vpn_summary()
        row = await self.vpn_fetchone(
            f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END), 0) AS ok_count,
                COALESCE(SUM(CASE WHEN latency_ms IS NOT NULL THEN 1 ELSE 0 END), 0) AS measured,
                AVG(latency_ms) AS avg_ms,
                MIN(latency_ms) AS min_ms,
                MAX(latency_ms) AS max_ms,
                COALESCE(SUM(CASE WHEN {_VPN_OK_PING_SQL} AND latency_ms < 100 THEN 1 ELSE 0 END), 0) AS bucket_0_100,
                COALESCE(SUM(CASE WHEN {_VPN_OK_PING_SQL} AND latency_ms >= 100 AND latency_ms < 500 THEN 1 ELSE 0 END), 0) AS bucket_100_500,
                COALESCE(SUM(CASE WHEN {_VPN_OK_PING_SQL} AND latency_ms >= 500 AND latency_ms < 1000 THEN 1 ELSE 0 END), 0) AS bucket_500_1000,
                COALESCE(SUM(CASE WHEN {_VPN_OK_PING_SQL} AND latency_ms >= 1000 THEN 1 ELSE 0 END), 0) AS bucket_1000,
                COALESCE(SUM(CASE WHEN NOT ({_VPN_OK_PING_SQL}) THEN 1 ELSE 0 END), 0) AS no_ping
            FROM vpn_latency_samples
            WHERE measured_at >= ? AND measured_at < ?
            """,
            (start, end),
        )
        total = int(row["total"]) if row else 0
        ok_count = int(row["ok_count"]) if row else 0
        measured = int(row["measured"]) if row else 0
        p95_ms = None
        p99_ms = None
        p99_9_ms = None
        p95_count = 0
        p99_count = 0
        p99_9_count = 0
        if measured > 0:
            p95_ms = await self._vpn_latency_percentile(start, end, 0.95, measured)
            p99_ms = await self._vpn_latency_percentile(start, end, 0.99, measured)
            p99_9_ms = await self._vpn_latency_percentile(start, end, 0.999, measured)
            counts = await self.vpn_fetchone(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN latency_ms >= ? THEN 1 ELSE 0 END), 0) AS p95_count,
                    COALESCE(SUM(CASE WHEN latency_ms >= ? THEN 1 ELSE 0 END), 0) AS p99_count,
                    COALESCE(SUM(CASE WHEN latency_ms >= ? THEN 1 ELSE 0 END), 0) AS p99_9_count
                FROM vpn_latency_samples
                WHERE latency_ms IS NOT NULL
                  AND measured_at >= ? AND measured_at < ?
                """,
                (p95_ms, p99_ms, p99_9_ms, start, end),
            )
            if counts:
                p95_count = int(counts["p95_count"])
                p99_count = int(counts["p99_count"])
                p99_9_count = int(counts["p99_9_count"])
        return {
            "total": total,
            "ok_count": ok_count,
            "fail_count": total - ok_count,
            "measured": measured,
            "avg_ms": row["avg_ms"] if row else None,
            "min_ms": row["min_ms"] if row else None,
            "max_ms": row["max_ms"] if row else None,
            "p95_ms": p95_ms,
            "p99_ms": p99_ms,
            "p99_9_ms": p99_9_ms,
            "p95_count": p95_count,
            "p99_count": p99_count,
            "p99_9_count": p99_9_count,
            "bucket_0_100": int(row["bucket_0_100"]) if row else 0,
            "bucket_100_500": int(row["bucket_100_500"]) if row else 0,
            "bucket_500_1000": int(row["bucket_500_1000"]) if row else 0,
            "bucket_1000": int(row["bucket_1000"]) if row else 0,
            "no_ping": int(row["no_ping"]) if row else 0,
        }

    async def _vpn_latency_percentile(self, start: str, end: str, p: float, measured: int) -> int | None:
        offset = min(measured - 1, max(0, math.ceil(p * measured) - 1))
        row = await self.vpn_fetchone(
            """
            SELECT latency_ms FROM vpn_latency_samples
            WHERE latency_ms IS NOT NULL
              AND measured_at >= ? AND measured_at < ?
            ORDER BY latency_ms
            LIMIT 1 OFFSET ?
            """,
            (start, end, offset),
        )
        return row["latency_ms"] if row else None

    async def vpn_top_nodes(self, start: str, end: str, limit: int = 5) -> list[dict[str, Any]]:
        if self.db.vpn_db is None:
            return []
        rows = await self.vpn_fetchall(
            """
            SELECT
                node_name,
                subscription,
                COUNT(*) AS samples,
                AVG(CASE WHEN ok = 1 THEN latency_ms END) AS avg_ms,
                MIN(CASE WHEN ok = 1 THEN latency_ms END) AS min_ms,
                MAX(CASE WHEN ok = 1 THEN latency_ms END) AS max_ms,
                SUM(CASE WHEN ok = 1 THEN 0 ELSE 1 END) AS fail_count
            FROM vpn_latency_samples
            WHERE measured_at >= ? AND measured_at < ?
            GROUP BY node_name, subscription
            ORDER BY samples DESC
            LIMIT ?
            """,
            (start, end, limit),
        )
        result = [dict(r) for r in rows]
        if not result:
            return result
        clauses: list[str] = []
        params: list[Any] = [start, end]
        for item in result:
            clauses.append("(node_name IS ? AND subscription IS ?)")
            params.extend([item["node_name"], item["subscription"]])
        lat_rows = await self.vpn_fetchall(
            f"""
            SELECT node_name, subscription, latency_ms
            FROM vpn_latency_samples
            WHERE measured_at >= ? AND measured_at < ?
              AND ok = 1 AND latency_ms IS NOT NULL
              AND ({" OR ".join(clauses)})
            """,
            params,
        )
        grouped: dict[tuple[Any, Any], list[int]] = defaultdict(list)
        for sample in lat_rows:
            grouped[(sample["node_name"], sample["subscription"])].append(int(sample["latency_ms"]))
        _apply_vpn_tail_stats(result, grouped, lambda item: (item["node_name"], item["subscription"]))
        return result

    async def vpn_top_subscriptions(self, start: str, end: str, limit: int = 5) -> list[dict[str, Any]]:
        if self.db.vpn_db is None:
            return []
        rows = await self.vpn_fetchall(
            """
            SELECT
                subscription,
                COUNT(*) AS samples,
                AVG(CASE WHEN ok = 1 THEN latency_ms END) AS avg_ms,
                MIN(CASE WHEN ok = 1 THEN latency_ms END) AS min_ms,
                MAX(CASE WHEN ok = 1 THEN latency_ms END) AS max_ms,
                SUM(CASE WHEN ok = 1 THEN 0 ELSE 1 END) AS fail_count
            FROM vpn_latency_samples
            WHERE measured_at >= ? AND measured_at < ?
            GROUP BY subscription
            ORDER BY samples DESC
            LIMIT ?
            """,
            (start, end, limit),
        )
        result = [dict(r) for r in rows]
        if not result:
            return result
        clauses = ["(subscription IS ?)"] * len(result)
        params: list[Any] = [start, end, *[item["subscription"] for item in result]]
        lat_rows = await self.vpn_fetchall(
            f"""
            SELECT subscription, latency_ms
            FROM vpn_latency_samples
            WHERE measured_at >= ? AND measured_at < ?
              AND ok = 1 AND latency_ms IS NOT NULL
              AND ({" OR ".join(clauses)})
            """,
            params,
        )
        grouped: dict[Any, list[int]] = defaultdict(list)
        for sample in lat_rows:
            grouped[sample["subscription"]].append(int(sample["latency_ms"]))
        _apply_vpn_tail_stats(result, grouped, lambda item: item["subscription"])
        return result
