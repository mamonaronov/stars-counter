"""User-facing text helpers."""

from __future__ import annotations

import html


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def percent_text(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".replace(".", ",") + "%"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def events_word(n: int) -> str:
    return plural_ru(n, "занятие", "занятия", "занятий")


def students_word(n: int) -> str:
    return plural_ru(n, "студент", "студента", "студентов")


def short_name(full_name: str) -> str:
    """'Иванов Иван Иванович' -> 'Иванов И.И.'; two words -> 'Иванов И.'."""
    parts = [p for p in full_name.split() if p]
    if not parts:
        return full_name
    if len(parts) == 1:
        return parts[0]
    initials = "".join(f"{p[0].upper()}." for p in parts[1:3])
    return f"{parts[0]} {initials}"


def truncate(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def course_text(enrollment_year: int | None, today_year: int, today_month: int) -> str:
    if not enrollment_year:
        return "—"
    course = today_year - enrollment_year + (1 if today_month >= 9 else 0)
    if course < 1:
        course = 1
    return f"{course} курс ({enrollment_year})"
