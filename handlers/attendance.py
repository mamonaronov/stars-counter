"""Public attendance view: any user can look up any student for any period."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
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
from services.periods import find_period, period_button_label
from services.roles import Actor
from utils.callbacks import NAV_MAIN, SEC_ATTENDANCE, SEC_MY
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


async def show_periods_for_student(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, student_id: int) -> None:
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
    b.row(*nav_row(SEC_ATTENDANCE))
    await respond(cb, f"{student_line(student, today)}\n\nВыберите месяц:", b.as_markup())


@router.callback_query(F.data.startswith(f"pk:{PICK_CTX}:"))
async def pick_student(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config) -> None:
    student_id = int(cb.data.rsplit(":", 1)[1])
    await show_periods_for_student(cb, state, repo, config, student_id)


@router.callback_query(F.data == SEC_MY)
async def open_my(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    await show_periods_for_student(cb, state, repo, config, actor.student.id)


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
    await state.clear()
    stats = await student_period_stats(repo, student, period)
    today = local_today(config.timezone)
    b = InlineKeyboardBuilder()
    others = [p for p in reversed(periods) if p.key != period.key]
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
