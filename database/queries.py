"""Parameterized data-access layer."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Iterable

import aiosqlite

from database.database import Database
from database.models import (
    Attendance,
    AuditEntry,
    Event,
    EventTemplate,
    Excuse,
    Request,
    Student,
    User,
    VerificationLink,
)
from utils.time import now_utc, to_iso

logger = logging.getLogger(__name__)


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

    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        async with self.conn.execute(sql, params) as cur:
            last_id = cur.lastrowid
        await self.conn.commit()
        return int(last_id or 0)

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

    # --------------------------------------------------------------- students

    async def create_student(
        self,
        full_name: str,
        enrollment_year: int | None,
        joined_on: str,
        created_by: int | None,
    ) -> Student:
        ts = to_iso(now_utc())
        new_id = await self._execute(
            """
            INSERT INTO students (full_name, enrollment_year, joined_on, status, created_by, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?)
            """,
            (full_name.strip(), enrollment_year, joined_on, created_by, ts, ts),
        )
        student = await self.get_student(new_id)
        assert student is not None
        return student

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
        return _many(
            Student,
            await self._fetchall(
                "SELECT * FROM students WHERE role IS NOT NULL AND status = 'active' ORDER BY full_name COLLATE NOCASE"
            ),
        )

    async def list_verified_students(self) -> list[Student]:
        return _many(
            Student,
            await self._fetchall(
                "SELECT * FROM students WHERE telegram_id IS NOT NULL AND status = 'active' ORDER BY full_name COLLATE NOCASE"
            ),
        )

    async def find_student_by_name(self, full_name: str, *, active_only: bool = True) -> Student | None:
        needle = " ".join(full_name.split()).casefold()
        for student in await self.list_students(active_only=active_only):
            if " ".join(student.full_name.split()).casefold() == needle:
                return student
        return None

    async def update_student(self, student_id: int, **fields: Any) -> Student | None:
        allowed = {"full_name", "enrollment_year", "joined_on", "left_on", "status", "role", "telegram_id", "verified_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
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
        return await self.update_student(student_id, role=role)

    async def bind_telegram(self, student_id: int, telegram_id: int) -> Student | None:
        return await self.update_student(student_id, telegram_id=telegram_id, verified_at=to_iso(now_utc()))

    async def unbind_telegram(self, student_id: int) -> Student | None:
        return await self.update_student(student_id, telegram_id=None, verified_at=None, role=None)

    # ------------------------------------------------------------ verification

    async def create_verification_link(self, student_id: int, created_by: int) -> VerificationLink:
        ts = to_iso(now_utc())
        await self._execute(
            "UPDATE verification_links SET revoked_at = ? WHERE student_id = ? AND used_at IS NULL AND revoked_at IS NULL",
            (ts, student_id),
        )
        token = secrets.token_urlsafe(18)
        await self._execute(
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

    async def use_verification_link(self, token: str, used_by: int) -> None:
        await self._execute(
            "UPDATE verification_links SET used_at = ?, used_by = ? WHERE token = ?",
            (to_iso(now_utc()), used_by, token),
        )

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
        return _many(
            EventTemplate,
            await self._fetchall("SELECT * FROM event_templates WHERE archived_at IS NULL ORDER BY title COLLATE NOCASE, id"),
        )

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

    async def delete_event(self, event_id: int) -> None:
        await self.update_event(event_id, deleted_at=to_iso(now_utc()), closes_month=0)

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
        exists = await self.is_present(event_id, student_id)
        if present and not exists:
            await self._execute(
                "INSERT INTO attendance (event_id, student_id, marked_by, marked_at) VALUES (?, ?, ?, ?)",
                (event_id, student_id, marked_by, to_iso(now_utc())),
            )
            return True
        if not present and exists:
            await self._execute(
                "DELETE FROM attendance WHERE event_id = ? AND student_id = ?", (event_id, student_id)
            )
            return True
        return False

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

    async def create_request(self, student_id: int, event_id: int, text: str) -> Request:
        new_id = await self._execute(
            "INSERT INTO requests (student_id, event_id, text, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (student_id, event_id, text.strip(), to_iso(now_utc())),
        )
        req = await self.get_request(new_id)
        assert req is not None
        return req

    async def get_request(self, request_id: int) -> Request | None:
        return _opt(Request, await self._fetchone("SELECT * FROM requests WHERE id = ?", (request_id,)))

    async def list_requests(self, *, status: str | None = "open", student_id: int | None = None, limit: int = 200) -> list[Request]:
        sql = "SELECT * FROM requests"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
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
        async with self.conn.execute(
            """
            UPDATE requests
            SET status = ?, resolved_by = ?, resolved_at = ?, resolution_comment = ?
            WHERE id = ? AND status = 'open'
            """,
            (status, resolved_by, to_iso(now_utc()), comment, request_id),
        ) as cur:
            changed = cur.rowcount
        await self.conn.commit()
        if not changed:
            return None
        return await self.get_request(request_id)

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

    async def claim_callback(self, callback_id: str, telegram_id: int) -> bool:
        try:
            await self._execute(
                "INSERT INTO callback_dedup (callback_id, telegram_id, created_at) VALUES (?, ?, ?)",
                (callback_id, telegram_id, to_iso(now_utc())),
            )
            return True
        except aiosqlite.IntegrityError:
            await self.conn.rollback()
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
