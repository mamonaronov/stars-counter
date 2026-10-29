"""Inline keyboards."""

from __future__ import annotations

from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import Event, EventTemplate, Student
from services.periods import Period, period_button_label
from services.roles import LEVEL_ADMIN, LEVEL_CHIEF_ADMIN, LEVEL_CHIEF_MARKER, LEVEL_MARKER, LEVEL_SUPERADMIN, Actor
from utils.callbacks import (
    NAV_CANCEL,
    NAV_MAIN,
    NOOP,
    SEC_ATTENDANCE,
    SEC_AUDIT,
    SEC_EVENTS,
    SEC_EXCUSES,
    SEC_HELP,
    SEC_MONTHS,
    SEC_MY,
    SEC_NEW_EVENT,
    SEC_NEW_REQUEST,
    SEC_REQUESTS,
    SEC_ROLES,
    SEC_SERVICE,
    SEC_STUDENTS,
    SEC_USAGE,
    SEC_WIPE,
    SEC_TEMPLATES,
    SEC_VERIFY,
)
from utils.formatting import truncate
from utils.time import MONTHS_RU_NOM, WEEKDAYS_RU, days_in_month, month_shift

PICK_PAGE = 16
LIST_PAGE = 10


def btn(text: str, data: str) -> InlineKeyboardButton:
    # Telegram rejects the whole keyboard when any label is longer than 64 characters.
    return InlineKeyboardButton(text=truncate(text, 64), callback_data=data)


def nav_row(back: str | None = None, *, menu: bool = True, back_text: str = "⬅️ Назад") -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if back:
        row.append(btn(back_text, back))
    if menu:
        row.append(btn("🏠 Меню", NAV_MAIN))
    return row


def back_kb(back: str | None = None, *, menu: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row = nav_row(back, menu=menu)
    if row:
        b.row(*row)
    return b.as_markup()


def cancel_kb(back: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("✖️ Отмена", back or NAV_CANCEL))
    return b.as_markup()


def confirm_kb(yes: str, no: str, *, yes_text: str = "✅ Подтвердить", no_text: str = "✖️ Отмена") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn(yes_text, yes), btn(no_text, no))
    return b.as_markup()


def main_menu(actor: Actor | None, *, open_requests: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    level = actor.level if actor else 0
    if actor is not None and actor.is_linked:
        b.row(btn("👤 Моя посещаемость", SEC_MY), btn("✉️ Запрос отмечающему", SEC_NEW_REQUEST))
    b.row(btn("📊 Посещаемость студентов", SEC_ATTENDANCE))
    if level >= LEVEL_MARKER:
        b.row(btn("➕ Новое занятие", SEC_NEW_EVENT), btn("📅 Занятия", SEC_EVENTS))
        req_label = f"📨 Запросы ({open_requests})" if open_requests else "📨 Запросы"
        b.row(btn("🗓 Месяцы и отчёты", SEC_MONTHS), btn(req_label, SEC_REQUESTS))
        b.row(btn("🩺 Больничные и отпросы", SEC_EXCUSES), btn("📋 Шаблоны", SEC_TEMPLATES))
        b.row(btn("🔗 Верификация", SEC_VERIFY), btn("🕵️ Журнал действий", SEC_AUDIT))
    if level >= LEVEL_CHIEF_MARKER:
        b.row(btn("👥 Студенты", SEC_STUDENTS), btn("🎖 Роли", SEC_ROLES))
    if level >= LEVEL_ADMIN:
        b.row(btn("📈 Статистика бота", SEC_USAGE))
    if level >= LEVEL_SUPERADMIN:
        b.row(btn("🛠 Сервис", SEC_SERVICE))
    if level >= LEVEL_CHIEF_ADMIN:
        b.row(btn("🧹 Очистить все данные", SEC_WIPE))
    b.row(btn("ℹ️ Помощь", SEC_HELP))
    return b.as_markup()


# ----------------------------------------------------------------- pickers


def students_picker_kb(
    students: list[Student],
    ctx: str,
    page: int,
    *,
    back: str | None = None,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    pages = max(1, (len(students) + PICK_PAGE - 1) // PICK_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = students[page * PICK_PAGE : (page + 1) * PICK_PAGE]
    row: list[InlineKeyboardButton] = []
    for student in chunk:
        name = truncate(student.full_name, 22 if not student.is_active else 32)
        if not student.is_active:
            name += " · удалён"
        row.append(btn(name, f"pk:{ctx}:{student.id}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    if pages > 1:
        b.row(
            btn("«", f"pkp:{ctx}:{page - 1}") if page > 0 else btn(" ", NOOP),
            btn(f"{page + 1}/{pages}", NOOP),
            btn("»", f"pkp:{ctx}:{page + 1}") if page < pages - 1 else btn(" ", NOOP),
        )
    for extra in extra_rows or []:
        b.row(*extra)
    b.row(*nav_row(back))
    return b.as_markup()


def periods_kb(periods: list[Period], prefix: str, *, back: str | None = None, newest_first: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    ordered = list(reversed(periods)) if newest_first else list(periods)
    for period in ordered:
        b.row(btn(period_button_label(period), f"{prefix}:{period.key}"))
    b.row(*nav_row(back))
    return b.as_markup()


# ------------------------------------------------------------------ events


def marking_kb(event: Event, students: list[Student], present: set[int], *, back: str) -> InlineKeyboardMarkup:
    """Students who already left or have not joined by the event date are marked with «·»."""
    b = InlineKeyboardBuilder()
    eligible = [s for s in students if s.counts_on(event.day)]
    skipped = [s for s in students if not s.counts_on(event.day)]

    def _add(group: list[Student], *, inactive: bool) -> None:
        row: list[InlineKeyboardButton] = []
        for student in group:
            if student.id in present:
                mark = "✅ "
            elif inactive:
                mark = "· "
            else:
                mark = ""
            row.append(btn(f"{mark}{truncate(student.full_name, 30)}", f"att:{event.id}:{student.id}"))
            if len(row) == 2:
                b.row(*row)
                row = []
        if row:
            b.row(*row)

    _add(eligible, inactive=False)
    _add(skipped, inactive=True)
    b.row(btn("✔️ Готово", back), btn("🏠 Меню", NAV_MAIN))
    return b.as_markup()


def events_list_kb(events: list[Event], counts: dict[int, int], page: int, *, prefix: str = "evp", open_prefix: str = "ev") -> InlineKeyboardMarkup:
    """events: newest first."""
    b = InlineKeyboardBuilder()
    pages = max(1, (len(events) + LIST_PAGE - 1) // LIST_PAGE)
    page = max(0, min(page, pages - 1))
    for event in events[page * LIST_PAGE : (page + 1) * LIST_PAGE]:
        kind = "" if event.is_mandatory else " (св.)"
        close = " 🔚" if event.closes_month else ""
        time = f" {event.start_time}" if event.start_time else ""
        label = f"{event.day.strftime('%d.%m')}{time} · {truncate(event.title, 22)}{kind} · {counts.get(event.id, 0)}{close}"
        b.row(btn(label, f"{open_prefix}:{event.id}"))
    if pages > 1:
        b.row(
            btn("«", f"{prefix}:{page - 1}") if page > 0 else btn(" ", NOOP),
            btn(f"{page + 1}/{pages}", NOOP),
            btn("»", f"{prefix}:{page + 1}") if page < pages - 1 else btn(" ", NOOP),
        )
    b.row(*nav_row())
    return b.as_markup()


def event_card_kb(event: Event, *, back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("✅ Отметить присутствие", f"mark:{event.id}"))
    b.row(btn("✏️ Название", f"eve:title:{event.id}"), btn("📅 Дата", f"eve:date:{event.id}"))
    b.row(btn("🕐 Время", f"eve:time:{event.id}"), btn("🔁 Тип", f"eve:kind:{event.id}"))
    if event.closes_month:
        b.row(btn("↩️ Не последнее в месяце", f"evc:off:{event.id}"))
    else:
        b.row(btn("🔚 Последнее в месяце", f"evc:on:{event.id}"))
    b.row(btn("📜 История изменений", f"evh:{event.id}:0"), btn("🗑 Удалить", f"evd:{event.id}"))
    b.row(*nav_row(back))
    return b.as_markup()


def templates_pick_kb(templates: list[EventTemplate], *, back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for tpl in templates:
        kind = "" if tpl.kind == "mandatory" else " (св.)"
        time = f" {tpl.default_time}" if tpl.default_time else ""
        b.row(btn(f"{truncate(tpl.title, 30)}{kind}{time}", f"newt:{tpl.id}"))
    b.row(btn("✍️ Без шаблона", "newt:0"))
    b.row(*nav_row(back))
    return b.as_markup()


def kind_kb(prefix: str, *, back: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("📘 Обязательное", f"{prefix}:mandatory"), btn("🟢 Свободное", f"{prefix}:free"))
    b.row(*nav_row(back))
    return b.as_markup()


def date_quick_kb(prefix: str, today: date, *, back: str | None = None, allow_future: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        btn("Сегодня", f"{prefix}:{today.isoformat()}"),
        btn("Вчера", f"{prefix}:{(today - timedelta(days=1)).isoformat()}"),
        btn("Позавчера", f"{prefix}:{(today - timedelta(days=2)).isoformat()}"),
    )
    b.row(btn("📅 Календарь", f"{prefix}m:{today.year:04d}-{today.month:02d}"))
    b.row(*nav_row(back))
    return b.as_markup()


def calendar_kb(year: int, month: int, prefix: str, *, back: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn(f"{MONTHS_RU_NOM[month]} {year}", NOOP))
    b.row(*[btn(d, NOOP) for d in WEEKDAYS_RU])
    first = date(year, month, 1)
    row: list[InlineKeyboardButton] = [btn(" ", NOOP) for _ in range(first.weekday())]
    for day in range(1, days_in_month(year, month) + 1):
        current = date(year, month, day)
        row.append(btn(str(day), f"{prefix}:{current.isoformat()}"))
        if len(row) == 7:
            b.row(*row)
            row = []
    if row:
        while len(row) < 7:
            row.append(btn(" ", NOOP))
        b.row(*row)
    py, pm = month_shift(year, month, -1)
    ny, nm = month_shift(year, month, 1)
    b.row(btn("«", f"{prefix}m:{py:04d}-{pm:02d}"), btn("»", f"{prefix}m:{ny:04d}-{nm:02d}"))
    b.row(*nav_row(back))
    return b.as_markup()


def time_kb(prefix: str, *, default_time: str | None = None, back: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row = [btn("🕐 Сейчас", f"{prefix}:now")]
    if default_time:
        row.append(btn(f"⏰ {default_time}", f"{prefix}:{default_time}"))
    row.append(btn("⏭ Без времени", f"{prefix}:none"))
    b.row(*row)
    b.row(*nav_row(back))
    return b.as_markup()


def _selected(text: str, on: bool) -> str:
    return f"[{text}]" if on else text


_VPN_SPAN_LABELS = {
    "5m": "5 мин",
    "30m": "30 мин",
    "1h": "час",
    "6h": "6 часов",
    "12h": "12 часов",
    "24h": "сутки",
    "7d": "неделю",
    "30d": "месяц",
    "all": "всё время",
}


def admin_vpn_kb(period: str = "24h", view: str = "n", *, rounded: bool = False) -> InlineKeyboardMarkup:
    if view not in {"n", "s", "a"}:
        view = "n"
    if view != "a":
        rounded = False
    token = "a:r" if view == "a" and rounded else view
    b = InlineKeyboardBuilder()
    rows = (
        (("5m", "5 мин"), ("30m", "30 мин"), ("1h", "1 ч")),
        (("6h", "6 ч"), ("12h", "12 ч"), ("24h", "сутки")),
        (("7d", "неделя"), ("30d", "месяц"), ("all", "всё время")),
    )
    for labels in rows:
        b.row(*[btn(_selected(label, key == period), f"adv:{key}:{token}") for key, label in labels])
    b.row(
        btn(_selected("Ноды", view == "n"), f"adv:{period}:n"),
        btn(_selected("Подписки", view == "s"), f"adv:{period}:s"),
        btn(_selected("Доступность", view == "a"), f"adv:{period}:a" + (":r" if rounded else "")),
    )
    span = _VPN_SPAN_LABELS.get(period, "сутки")
    if view == "a":
        b.row(btn(_selected("Округление", rounded), f"adv:{period}:a" + ("" if rounded else ":r")))
    b.row(btn(f"📄 Логи за {span}", f"advl:{period}"))
    if view == "a":
        b.row(btn(f"📈 Доступность за {span}", f"advc:{period}:a" + (":r" if rounded else "")))
    else:
        chart_view = ":s" if view == "s" else ""
        b.row(btn(f"📈 Картинки за {span}", f"advc:{period}{chart_view}"))
    b.row(btn(f"📈 Load avg за {span}", f"adld:{period}"))
    b.row(btn("🛠 Сервис", SEC_SERVICE))
    return b.as_markup()


def pager_row(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    return [
        btn("«", f"{prefix}:{page - 1}") if page > 0 else btn(" ", NOOP),
        btn(f"{page + 1}/{pages}", NOOP),
        btn("»", f"{prefix}:{page + 1}") if page < pages - 1 else btn(" ", NOOP),
    ]
