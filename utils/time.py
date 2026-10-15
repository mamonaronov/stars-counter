"""Timezone-aware time helpers. Stored timestamps are UTC ISO-8601, dates are ISO YYYY-MM-DD."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

MONTHS_RU_GEN = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

MONTHS_RU_NOM = [
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]

WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def to_local(dt: datetime, tz_name: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone(tz_name))


def local_now(tz_name: str) -> datetime:
    return now_utc().astimezone(zone(tz_name))


def local_today(tz_name: str) -> date:
    return local_now(tz_name).date()


def format_dt(dt: datetime, tz_name: str) -> str:
    local = to_local(dt, tz_name)
    return local.strftime("%d.%m.%Y %H:%M")


def format_dt_iso(value: str | None, tz_name: str) -> str:
    if not value:
        return "—"
    try:
        return format_dt(parse_iso(value), tz_name)
    except ValueError:
        return value


def format_date(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def format_date_short(day: date) -> str:
    return day.strftime("%d.%m")


def format_date_words(day: date) -> str:
    return f"{day.day} {MONTHS_RU_GEN[day.month]} {day.year}"


def month_title(day: date) -> str:
    return f"{MONTHS_RU_NOM[day.month]} {day.year}"


def parse_date_text(value: str, *, today: date | None = None) -> date:
    """Accept dd.mm.yyyy, dd.mm.yy, dd.mm (current year), yyyy-mm-dd."""
    raw = (value or "").strip().lower()
    base = today or date.today()
    if raw in {"сегодня", "today"}:
        return base
    if raw in {"вчера", "yesterday"}:
        return base - timedelta(days=1)
    if raw in {"позавчера"}:
        return base - timedelta(days=2)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return date.fromisoformat(raw)
    parts = re.split(r"[.\-/\s]+", raw)
    parts = [p for p in parts if p]
    if len(parts) == 2:
        day, month = int(parts[0]), int(parts[1])
        return date(base.year, month, day)
    if len(parts) == 3:
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        if year < 100:
            year += 2000
        return date(year, month, day)
    raise ValueError("date")


def parse_hhmm(value: str) -> str:
    raw = (value or "").strip().replace("：", ":")
    if not raw:
        raise ValueError("time")
    normalized = raw.replace(".", ":").replace("-", ":").replace(" ", ":")
    if ":" in normalized:
        parts = normalized.split(":")
        if len(parts) != 2:
            raise ValueError("time")
        hour, minute = int(parts[0]), int(parts[1])
    elif raw.isdigit() and len(raw) in {3, 4}:
        hour, minute = int(raw[:-2]), int(raw[-2:])
    elif raw.isdigit() and len(raw) <= 2:
        hour, minute = int(raw), 0
    else:
        raise ValueError("time")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time")
    return f"{hour:02d}:{minute:02d}"


def month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def days_in_month(year: int, month: int) -> int:
    ny, nm = month_shift(year, month, 1)
    return (date(ny, nm, 1) - timedelta(days=1)).day
