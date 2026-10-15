"""Shared handler helpers: main screen, permission checks, student picker, stats text."""

from __future__ import annotations

from datetime import date

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import Config
from database.models import Event, Student
from database.queries import Repo
from keyboards.main import main_menu, students_picker_kb
from services.periods import Period, build_periods, period_range_text
from services.roles import LEVEL_MARKER, Actor
from services.stats import STATUS_EXCUSED, STATUS_ICON, StudentStats, compute_student_stats
from states import PickSG
from utils.callbacks import NAV_MAIN
from utils.formatting import course_text, esc, percent_text
from utils.telegram import answer_callback, safe_edit
from utils.time import local_today

KIND_TITLES = {"mandatory": "обязательное", "free": "свободное"}
EXCUSE_TITLES = {"sick": "больничный", "leave": "отпрос"}

NO_ACCESS = "Недостаточно прав для этого действия."


def menu_text(actor: Actor | None, config: Config) -> str:
    lines = ["🎓 <b>Посещаемость</b>", ""]
    if actor is None:
        lines.append("Выберите действие:")
        return "\n".join(lines)
    if actor.is_superadmin:
        lines.append("Вы: <b>суперадмин</b>")
        if actor.student:
            lines.append(f"Студент: {esc(actor.student.full_name)}")
    elif actor.is_verified_student:
        lines.append(f"Вы: <b>{esc(actor.student.full_name)}</b> · {esc(actor.role_title)}")
    else:
        lines.append(
            "Вы не верифицированы. Чтобы бот узнал вас как студента, попросите отмечающего "
            "прислать ссылку для верификации."
        )
    lines.append("")
    lines.append("Выберите действие:")
    return "\n".join(lines)


async def show_main(
    target: CallbackQuery | Message,
    actor: Actor | None,
    config: Config,
    repo: Repo,
    state: FSMContext | None = None,
    *,
    notice: str | None = None,
) -> None:
    if state is not None:
        await state.clear()
    open_requests = 0
    if actor is not None and actor.can_mark:
        open_requests = await repo.count_open_requests()
    text = menu_text(actor, config)
    markup = main_menu(actor, open_requests=open_requests)
    if isinstance(target, CallbackQuery):
        await answer_callback(target, notice)
        await safe_edit(target.message, text, markup)
        return
    await target.answer(text, reply_markup=markup)


async def deny(event: CallbackQuery | Message, text: str = NO_ACCESS) -> None:
    if isinstance(event, CallbackQuery):
        await answer_callback(event, text, show_alert=True)
    else:
        await event.answer(text)


async def require_level(event: CallbackQuery | Message, actor: Actor | None, level: int = LEVEL_MARKER) -> bool:
    if actor is None or actor.level < level:
        await deny(event)
        return False
    return True


async def require_verified(event: CallbackQuery | Message, actor: Actor | None) -> bool:
    if actor is None or not actor.is_verified_student:
        await deny(event, "Эта функция доступна только верифицированным студентам.")
        return False
    return True


async def respond(
    target: CallbackQuery | Message,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    notice: str | None = None,
) -> None:
    if isinstance(target, CallbackQuery):
        await answer_callback(target, notice)
        await safe_edit(target.message, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- periods


async def load_periods(repo: Repo, config: Config) -> list[Period]:
    events = await repo.list_events()
    return build_periods(events, local_today(config.timezone))


def period_header(period: Period) -> str:
    status = "закрыт" if period.closed else "идёт"
    return f"📅 <b>{esc(period.title)}</b> ({period_range_text(period)}, {status})"


# ------------------------------------------------------------ student picker

PICK_FILTER_ONLY_VERIFIED = "verified"
PICK_FILTER_ONLY_UNVERIFIED = "unverified"


async def _picker_students(repo: Repo, data: dict) -> list[Student]:
    query = data.get("pick_query") or None
    students = await repo.list_students(active_only=not data.get("pick_include_removed"), query=query)
    only = data.get("pick_only")
    if only == PICK_FILTER_ONLY_VERIFIED:
        students = [s for s in students if s.is_verified]
    elif only == PICK_FILTER_ONLY_UNVERIFIED:
        students = [s for s in students if not s.is_verified]
    exclude = set(data.get("pick_exclude") or [])
    if exclude:
        students = [s for s in students if s.id not in exclude]
    return students


async def show_student_picker(
    target: CallbackQuery | Message,
    state: FSMContext,
    repo: Repo,
    *,
    ctx: str,
    title: str,
    back: str,
    page: int = 0,
    only: str | None = None,
    exclude: list[int] | None = None,
    include_removed: bool = False,
    keep: dict | None = None,
) -> None:
    """Render the paged list of students. A text message while in PickSG.filter narrows it."""
    await state.set_state(PickSG.filter)
    await state.update_data(
        pick_ctx=ctx,
        pick_title=title,
        pick_back=back,
        pick_only=only,
        pick_exclude=exclude or [],
        pick_include_removed=include_removed,
        pick_query=None,
        **(keep or {}),
    )
    await render_student_picker(target, state, repo, page=page)


async def render_student_picker(target: CallbackQuery | Message, state: FSMContext, repo: Repo, *, page: int = 0) -> None:
    data = await state.get_data()
    students = await _picker_students(repo, data)
    title = data.get("pick_title") or "Выберите студента"
    query = data.get("pick_query")
    text = f"{title}\n\n"
    if query:
        text += f"Фильтр: «{esc(query)}» · найдено {len(students)}\n"
    text += "Можно отправить часть фамилии сообщением, чтобы отфильтровать список."
    if not students:
        text += "\n\nНикого не найдено."
    markup = students_picker_kb(students, data.get("pick_ctx", "x"), page, back=data.get("pick_back") or NAV_MAIN)
    await respond(target, text, markup)


# ----------------------------------------------------------------- stats


def student_line(student: Student, today: date) -> str:
    course = course_text(student.enrollment_year, today.year, today.month)
    return f"👤 <b>{esc(student.full_name)}</b> · {course}"


def stats_text(stats: StudentStats, period: Period, today: date, *, show_events: bool = True) -> str:
    lines = [student_line(stats.student, today), period_header(period), ""]
    lines.append(f"Процент посещаемости: <b>{percent_text(stats.percent)}</b>")
    lines.append(
        f"Обязательных занятий: {stats.mandatory_total} · был: {stats.attended_mandatory} · "
        f"по уважительной: {stats.excused} · свободных посетил: {stats.attended_free}"
    )
    if stats.mandatory_total == 0:
        lines.append("Обязательных занятий в периоде пока нет, процент не считается.")
    if show_events and stats.rows:
        lines.append("")
        lines.append("Занятия:")
        for row in stats.rows:
            event = row.event
            kind = "" if event.is_mandatory else " (св.)"
            time = f" {event.start_time}" if event.start_time else ""
            suffix = ""
            if row.status == STATUS_EXCUSED and row.excuse is not None:
                suffix = f" — {EXCUSE_TITLES.get(row.excuse.kind, row.excuse.kind)}"
            lines.append(f"{STATUS_ICON[row.status]} {event.day.strftime('%d.%m')}{time} {esc(event.title)}{kind}{suffix}")
        lines.append("")
        lines.append("✅ был · ❌ пропуск · 🩺 уважительная причина · ➖ свободное, не был · ⚪ не учитывается")
    return "\n".join(lines)


async def student_period_stats(repo: Repo, student: Student, period: Period) -> StudentStats:
    present = await repo.attendance_of_student(student.id)
    excuses = await repo.list_excuses(active_only=True, student_id=student.id)
    return compute_student_stats(student, period.events, present, excuses)


def event_title_line(event: Event) -> str:
    time = f" {event.start_time}" if event.start_time else ""
    return f"{event.day.strftime('%d.%m.%Y')}{time} · {esc(event.title)} ({KIND_TITLES[event.kind]})"
