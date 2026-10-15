"""Row models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(slots=True)
class User:
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    first_seen_at: str
    last_seen_at: str

    @property
    def display(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        if self.username:
            return f"{name} (@{self.username})" if name else f"@{self.username}"
        return name or str(self.telegram_id)


@dataclass(slots=True)
class Student:
    id: int
    full_name: str
    enrollment_year: int | None
    telegram_id: int | None
    verified_at: str | None
    role: str | None
    joined_on: str
    left_on: str | None
    status: str
    created_by: int | None
    created_at: str
    updated_at: str

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_verified(self) -> bool:
        return self.telegram_id is not None

    @property
    def joined_date(self) -> date:
        return date.fromisoformat(self.joined_on)

    @property
    def left_date(self) -> date | None:
        return date.fromisoformat(self.left_on) if self.left_on else None

    def counts_on(self, day: date) -> bool:
        """Whether an event on *day* is part of this student's statistics."""
        if day < self.joined_date:
            return False
        left = self.left_date
        return left is None or day <= left


@dataclass(slots=True)
class VerificationLink:
    token: str
    student_id: int
    created_by: int
    created_at: str
    used_at: str | None
    used_by: int | None
    revoked_at: str | None


@dataclass(slots=True)
class EventTemplate:
    id: int
    title: str
    kind: str
    default_time: str | None
    created_by: int
    created_at: str
    archived_at: str | None


@dataclass(slots=True)
class Event:
    id: int
    title: str
    kind: str
    event_date: str
    start_time: str | None
    template_id: int | None
    closes_month: int
    month_title: str | None
    created_by: int
    created_at: str
    updated_at: str
    deleted_at: str | None

    @property
    def day(self) -> date:
        return date.fromisoformat(self.event_date)

    @property
    def is_mandatory(self) -> bool:
        return self.kind == "mandatory"

    @property
    def is_free(self) -> bool:
        return self.kind == "free"

    @property
    def sort_key(self) -> tuple:
        return (self.event_date, self.start_time or "", self.id)


@dataclass(slots=True)
class Attendance:
    event_id: int
    student_id: int
    marked_by: int
    marked_at: str


@dataclass(slots=True)
class Excuse:
    id: int
    student_id: int
    kind: str
    date_from: str
    date_to: str
    comment: str | None
    created_by: int
    created_at: str
    cancelled_at: str | None
    cancelled_by: int | None

    @property
    def is_active(self) -> bool:
        return self.cancelled_at is None

    def covers(self, day: date) -> bool:
        return self.is_active and date.fromisoformat(self.date_from) <= day <= date.fromisoformat(self.date_to)


@dataclass(slots=True)
class Request:
    id: int
    student_id: int
    event_id: int
    text: str
    status: str
    created_at: str
    resolved_by: int | None
    resolved_at: str | None
    resolution_comment: str | None


@dataclass(slots=True)
class AuditEntry:
    id: int
    actor_telegram_id: int
    actor_student_id: int | None
    actor_name: str | None
    actor_role: str
    action: str
    entity_type: str | None
    entity_id: int | None
    details: str | None
    created_at: str


@dataclass(slots=True)
class VpnLatencySample:
    id: int
    measured_at: str
    ok: int
    latency_ms: int | None
    node_name: str | None
    subscription: str | None
    error: str | None
    host_uptime_s: float | None = None
