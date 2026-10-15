"""Attendance percentage.

percent = (attended mandatory + attended free + excused mandatory misses) / mandatory total * 100

* free events a student skipped are ignored; attended free events add to the numerator,
  so the percentage can exceed 100;
* mandatory events missed during an active sick leave / approved absence count as attended;
* only events on/after the student's ``joined_on`` (and before ``left_on``) are counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from database.models import Event, Excuse, Student

STATUS_PRESENT = "present"
STATUS_ABSENT = "absent"
STATUS_EXCUSED = "excused"
STATUS_FREE_SKIPPED = "free_skipped"
STATUS_NOT_COUNTED = "not_counted"

STATUS_ICON = {
    STATUS_PRESENT: "✅",
    STATUS_ABSENT: "❌",
    STATUS_EXCUSED: "🩺",
    STATUS_FREE_SKIPPED: "➖",
    STATUS_NOT_COUNTED: "⚪",
}


@dataclass(slots=True)
class EventRow:
    event: Event
    status: str
    excuse: Excuse | None = None


@dataclass(slots=True)
class StudentStats:
    student: Student
    mandatory_total: int = 0
    attended_mandatory: int = 0
    attended_free: int = 0
    excused: int = 0
    rows: list[EventRow] = field(default_factory=list)

    @property
    def numerator(self) -> int:
        return self.attended_mandatory + self.attended_free + self.excused

    @property
    def percent(self) -> float | None:
        if self.mandatory_total <= 0:
            return None
        return round(self.numerator / self.mandatory_total * 100, 2)

    @property
    def percent_or_zero(self) -> float:
        return self.percent if self.percent is not None else 0.0


def _excuse_for(excuses: list[Excuse], event: Event) -> Excuse | None:
    for excuse in excuses:
        if excuse.covers(event.day):
            return excuse
    return None


def compute_student_stats(
    student: Student,
    events: list[Event],
    present_event_ids: set[int],
    excuses: list[Excuse],
) -> StudentStats:
    stats = StudentStats(student=student)
    for event in events:
        if not student.counts_on(event.day):
            stats.rows.append(EventRow(event, STATUS_NOT_COUNTED))
            continue
        present = event.id in present_event_ids
        if event.is_mandatory:
            stats.mandatory_total += 1
            if present:
                stats.attended_mandatory += 1
                stats.rows.append(EventRow(event, STATUS_PRESENT))
                continue
            excuse = _excuse_for(excuses, event)
            if excuse is not None:
                stats.excused += 1
                stats.rows.append(EventRow(event, STATUS_EXCUSED, excuse))
            else:
                stats.rows.append(EventRow(event, STATUS_ABSENT))
        else:
            if present:
                stats.attended_free += 1
                stats.rows.append(EventRow(event, STATUS_PRESENT))
            else:
                stats.rows.append(EventRow(event, STATUS_FREE_SKIPPED))
    return stats


def compute_group_stats(
    students: list[Student],
    events: list[Event],
    attendance: dict[int, set[int]],
    excuses: list[Excuse],
) -> list[StudentStats]:
    """attendance: event_id -> set(student_id). Result sorted by percent desc, then name."""
    by_student: dict[int, set[int]] = {}
    for event_id, student_ids in attendance.items():
        for sid in student_ids:
            by_student.setdefault(sid, set()).add(event_id)
    excuses_by_student: dict[int, list[Excuse]] = {}
    for excuse in excuses:
        if excuse.is_active:
            excuses_by_student.setdefault(excuse.student_id, []).append(excuse)
    result = [
        compute_student_stats(s, events, by_student.get(s.id, set()), excuses_by_student.get(s.id, []))
        for s in students
    ]
    result.sort(key=lambda item: (-(item.percent_or_zero), item.student.full_name.lower()))
    return result
