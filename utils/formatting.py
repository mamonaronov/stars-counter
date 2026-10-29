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


def _paren_suffix(value: str) -> tuple[str, str | None]:
    prefix, sep, rest = value.rpartition(" (")
    if not sep or not rest.endswith(")"):
        return value, None
    return prefix, rest[:-1]


def colon_block(rows: list[tuple[str, str]]) -> str:
    """Right-align labels so colons share one column in a monospace font.

    Values that end with a parenthetical suffix keep those suffixes in a
    second column, with the text inside the parentheses right-aligned.
    """
    if not rows:
        return ""
    label_width = max(len(label) for label, _ in rows)
    parts = [_paren_suffix(value) for _, value in rows]
    prefix_width = max((len(prefix) for prefix, inner in parts if inner is not None), default=0)
    inner_width = max((len(inner) for _, inner in parts if inner is not None), default=0)
    lines: list[str] = []
    for (label, _), (prefix, inner) in zip(rows, parts, strict=True):
        if inner is None:
            value = prefix
        else:
            value = f"{prefix.ljust(prefix_width)} ({inner.rjust(inner_width)})"
        lines.append(f"{label.rjust(label_width)}: {value}")
    return "\n".join(lines)


def pre_html(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def seconds_human(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs and not days:
        parts.append(f"{secs} с")
    if not parts:
        return "0 с"
    return " ".join(parts)


def course_text(enrollment_year: int | None, today_year: int, today_month: int) -> str:
    if not enrollment_year:
        return "—"
    course = today_year - enrollment_year + (1 if today_month >= 9 else 0)
    if course < 1:
        course = 1
    return f"{course} курс ({enrollment_year})"
