"""Public attendance view: any user can look up any student for any period."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.models import Student
from database.queries import Repo
from handlers.common import (
    load_periods,
    require_verified,
    respond,
    show_student_picker,
    stats_text,
    student_line,
    student_period_stats,
)
from keyboards.main import btn, nav_row
from services.periods import OPEN_KEY, Period, find_period, period_button_label, previous_closed_period
from services.roles import Actor
from utils.callbacks import NAV_MAIN, SEC_ATTENDANCE, SEC_MY, SEC_MY_PREV
from utils.formatting import truncate
from utils.telegram import answer_callback
from utils.time import local_today

router = Router(name="attendance")

PICK_CTX = "att"


@router.callback_query(F.data == SEC_ATTENDANCE)
async def open_attendance(cb: CallbackQuery, state: FSMContext, repo: Repo) -> None:
    await show_student_picker(
        cb,
        state,
        repo,
        ctx=PICK_CTX,
        title="📊 <b>Посещаемость</b>\nВыберите студента:",
        back=NAV_MAIN,
        include_removed=True,
    )


def _own_student(actor: Actor | None, student_id: int) -> bool:
    return actor is not None and actor.student is not None and actor.student.id == student_id


async def show_periods_for_student(
    cb: CallbackQuery,
    state: FSMContext,
    repo: Repo,
    config: Config,
    student_id: int,
    *,
    back: str = SEC_ATTENDANCE,
) -> None:
    student = await repo.get_student(student_id)
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    await state.clear()
    periods = await load_periods(repo, config)
    today = local_today(config.timezone)
    b = InlineKeyboardBuilder()
    for period in reversed(periods):
        b.row(btn(period_button_label(period), f"pa:{student.id}:{period.key}"))
    b.row(*nav_row(back))
    await respond(cb, f"{student_line(student, today)}\n\nВыберите месяц:", b.as_markup())


@router.callback_query(F.data.startswith(f"pk:{PICK_CTX}:"))
async def pick_student(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    student_id = int(cb.data.rsplit(":", 1)[1])
    back = SEC_MY if _own_student(actor, student_id) else SEC_ATTENDANCE
    await show_periods_for_student(cb, state, repo, config, student_id, back=back)


async def show_student_period(
    cb: CallbackQuery,
    state: FSMContext,
    repo: Repo,
    config: Config,
    actor: Actor | None,
    student: Student,
    period: Period,
    periods: list[Period],
) -> None:
    await state.clear()
    stats = await student_period_stats(repo, student, period)
    today = local_today(config.timezone)
    b = InlineKeyboardBuilder()
    if _own_student(actor, student.id):
        switch = (
            btn("📅 Прошлый", SEC_MY_PREV)
            if period.key == OPEN_KEY
            else btn("📅 Текущий", SEC_MY)
        )
        b.row(btn("📆 Все месяцы", f"pk:{PICK_CTX}:{student.id}"), switch, btn("🏠 Меню", NAV_MAIN))
    else:
        others = [item for item in reversed(periods) if item.key != period.key]
        row = []
        for other in others[:6]:
            row.append(btn(truncate(other.title, 40), f"pa:{student.id}:{other.key}"))
            if len(row) == 2:
                b.row(*row)
                row = []
        if row:
            b.row(*row)
        b.row(btn("📆 Все месяцы", f"pk:{PICK_CTX}:{student.id}"), *nav_row(SEC_ATTENDANCE))
    await respond(cb, stats_text(stats, period, today, reveal_excuses=bool(actor and actor.can_mark)), b.as_markup())


@router.callback_query(F.data == SEC_MY)
async def open_my(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    student = await repo.get_student(actor.student.id)
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    periods = await load_periods(repo, config)
    current = find_period(periods, OPEN_KEY)
    if current is None:
        await answer_callback(cb, "Текущего месяца нет", show_alert=True)
        return
    await show_student_period(cb, state, repo, config, actor, student, current, periods)


@router.callback_query(F.data == SEC_MY_PREV)
async def open_my_previous(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    student = await repo.get_student(actor.student.id)
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    periods = await load_periods(repo, config)
    previous = previous_closed_period(periods)
    if previous is None:
        await answer_callback(cb, "Прошлого месяца ещё нет.", show_alert=True)
        return
    await show_student_period(cb, state, repo, config, actor, student, previous, periods)


@router.callback_query(F.data.startswith("pa:"))
async def show_period_stats(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    _, raw_id, key = cb.data.split(":", 2)
    student = await repo.get_student(int(raw_id))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    await show_student_period(cb, state, repo, config, actor, student, period, periods)
