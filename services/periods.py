"""Reporting periods ("months") are defined by closing events, not the calendar."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from database.models import Event, Student
from utils.formatting import truncate
from utils.time import month_title

OPEN_KEY = "open"


@dataclass(slots=True)
class Period:
    key: str
    title: str
    events: list[Event] = field(default_factory=list)
    closer: Event | None = None

    @property
    def closed(self) -> bool:
        return self.closer is not None

    @property
    def start_date(self) -> date | None:
        return self.events[0].day if self.events else None

    @property
    def end_date(self) -> date | None:
        return self.events[-1].day if self.events else None

    @property
    def mandatory_count(self) -> int:
        return sum(1 for e in self.events if e.is_mandatory)

    @property
    def free_count(self) -> int:
        return sum(1 for e in self.events if e.is_free)

    def contains(self, event_id: int) -> bool:
        return any(e.id == event_id for e in self.events)


def period_title_for(closer: Event) -> str:
    return closer.month_title or month_title(closer.day)


def open_period_title(events: list[Event], today: date) -> str:
    if events:
        return f"Текущий: {month_title(events[-1].day)}"
    return f"Текущий: {month_title(today)}"


def build_periods(events: list[Event], today: date) -> list[Period]:
    """Chronological list of periods; the last one is always the open period."""
    ordered = sorted(events, key=lambda e: e.sort_key)
    periods: list[Period] = []
    bucket: list[Event] = []
    for event in ordered:
        bucket.append(event)
        if event.closes_month:
            periods.append(Period(key=str(event.id), title=period_title_for(event), events=bucket, closer=event))
            bucket = []
    periods.append(Period(key=OPEN_KEY, title=open_period_title(bucket, today), events=bucket, closer=None))
    return periods


def find_period(periods: list[Period], key: str) -> Period | None:
    for period in periods:
        if period.key == key:
            return period
    return None


def period_of_event(periods: list[Period], event_id: int) -> Period | None:
    for period in periods:
        if period.contains(event_id):
            return period
    return None


def period_index(periods: list[Period], key: str) -> int:
    for index, period in enumerate(periods):
        if period.key == key:
            return index
    return -1


def period_button_label(period: Period) -> str:
    """Button text must stay within Telegram's 64-character limit."""
    suffix = f" · {len(period.events)} зан."
    return truncate(period.title, 64 - len(suffix)) + suffix


def students_for_period(students: list[Student], period: Period) -> list[Student]:
    """Students who overlap the period. An open period without events keeps active students."""
    start, end = period.start_date, period.end_date
    result: list[Student] = []
    for student in students:
        if start is None or end is None:
            if student.is_active:
                result.append(student)
            continue
        if not student.is_active and (student.left_date is None or student.left_date < start):
            continue
        if student.joined_date > end:
            continue
        result.append(student)
    return result


def period_range_text(period: Period) -> str:
    start, end = period.start_date, period.end_date
    if start is None or end is None:
        return "нет занятий"
    return f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"
