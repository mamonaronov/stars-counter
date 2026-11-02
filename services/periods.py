"""Reporting periods ("months") are defined by closing events, not the calendar."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from database.models import Event, Student
from database.queries import MonthBoundaryError, Repo
from utils.formatting import truncate
from utils.time import month_title

OPEN_KEY = "open"
# The open month ends on this day once a lesson exists. Stored in system_info.
OPEN_MONTH_END_KEY = "open_month_ends_on"


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


def previous_closed_period(periods: list[Period]) -> Period | None:
    """The last closed period. The open period is always the final item."""
    if len(periods) < 2:
        return None
    period = periods[-2]
    return period if period.closed else None


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


def lesson_that_closes_on(events: list[Event], end_on: date) -> Event | None:
    """Last lesson on the scheduled end day. That lesson is the month boundary."""
    on_day = [event for event in events if event.day == end_on]
    if not on_day:
        return None
    return max(on_day, key=lambda event: event.sort_key)


def scheduled_end_is_pending(periods: list[Period], end_on: date) -> bool:
    """The date is still ahead of the last closed month, so the open month can use it."""
    previous = previous_closed_period(periods)
    if previous is None or previous.closer is None:
        return True
    return end_on > previous.closer.day


def month_end_date_is_taken(periods: list[Period], end_on: date) -> bool:
    """The date already belongs to a closed month."""
    previous = previous_closed_period(periods)
    if previous is None or previous.closer is None:
        return False
    return end_on < previous.closer.day


async def scheduled_month_end(repo: Repo) -> date | None:
    raw = (await repo.db.get_system(OPEN_MONTH_END_KEY) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def set_scheduled_month_end(repo: Repo, day: date | None) -> None:
    await repo.db.set_system(OPEN_MONTH_END_KEY, "" if day is None else day.isoformat())


async def drop_scheduled_month_end_unless(repo: Repo, day: date) -> None:
    """A boundary picked by hand replaces a scheduled end on another day."""
    planned = await scheduled_month_end(repo)
    if planned is not None and planned != day:
        await set_scheduled_month_end(repo, None)


async def ensure_scheduled_month_end(repo: Repo) -> Event | None:
    """Close the open month on the last lesson of the scheduled day, if that lesson exists.

    A later lesson the same day takes the boundary over. A closer after that day is left
    alone. Returns the lesson when this call moved the boundary onto it.
    """
    end_on = await scheduled_month_end(repo)
    if end_on is None:
        return None
    events = await repo.list_events()
    if any(event.closes_month and event.day > end_on for event in events):
        return None
    target = lesson_that_closes_on(events, end_on)
    if target is None or target.closes_month:
        return None
    same_day = next(
        (event for event in events if event.closes_month and event.day == end_on and event.id != target.id),
        None,
    )
    try:
        await repo.set_month_closer(
            target.id,
            previous_closer_id=same_day.id if same_day is not None else None,
            month_title=same_day.month_title if same_day is not None else None,
        )
    except MonthBoundaryError:
        return None
    return target


def period_range_text(period: Period) -> str:
    start, end = period.start_date, period.end_date
    if start is None or end is None:
        return "нет занятий"
    return f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"
