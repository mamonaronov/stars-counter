"""Reporting periods: close a month, move its end, rename, reopen, Excel report."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.queries import Repo
from handlers.common import event_title_line, load_periods, period_header, require_level, respond
from keyboards.main import btn, cancel_kb, confirm_kb, nav_row, periods_kb
from services.audit import audit
from services.periods import OPEN_KEY, Period, find_period, period_index, students_for_period
from services.report import build_report_xlsx
from services.roles import Actor
from services.stats import StudentStats, compute_group_stats
from states import MonthSG
from utils.callbacks import SEC_MONTHS
from utils.formatting import esc, percent_text
from utils.telegram import answer_callback, bytes_file

router = Router(name="months")


# ---------------------------------------------------------------- helpers


async def period_group_stats(repo: Repo, period: Period) -> list[StudentStats]:
    students = students_for_period(await repo.list_students(active_only=False), period)
    attendance = await repo.attendance_all()
    excuses = await repo.list_excuses(active_only=True)
    return compute_group_stats(students, period.events, attendance, excuses)


async def show_months(target: CallbackQuery | Message, repo: Repo, config: Config) -> None:
    periods = await load_periods(repo, config)
    text = (
        "🗓 <b>Месяцы и отчёты</b>\n\n"
        "Месяц заканчивается занятием, которое отмечено как последнее. "
        "Следующее занятие автоматически открывает новый месяц."
    )
    await respond(target, text, periods_kb(periods, "mon"))


async def show_month_card(target: CallbackQuery | Message, repo: Repo, config: Config, key: str, *, notice: str | None = None) -> None:
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        if isinstance(target, CallbackQuery):
            await answer_callback(target, "Период не найден", show_alert=True)
        return
    lines = [
        period_header(period),
        f"Занятий: {len(period.events)} (обязательных {period.mandatory_count}, свободных {period.free_count})",
    ]
    if period.closer:
        lines.append(f"Последнее занятие: {event_title_line(period.closer)}")
    b = InlineKeyboardBuilder()
    b.row(btn("📑 Отчёт Excel", f"monx:{period.key}"), btn("📊 Сводка", f"mons:{period.key}"))
    if period.closed:
        b.row(btn("🔁 Другое последнее занятие", f"monc:{period.key}"))
        b.row(btn("✏️ Переименовать", f"monr:{period.key}"), btn("❌ Открыть обратно", f"mono:{period.key}"))
    elif period.events:
        b.row(btn("🔚 Закрыть месяц", f"monc:{period.key}"))
    b.row(*nav_row(SEC_MONTHS))
    await respond(target, "\n".join(lines), b.as_markup(), notice=notice)


# ------------------------------------------------------------------ list


@router.callback_query(F.data == SEC_MONTHS)
async def open_months(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_months(cb, repo, config)


@router.callback_query(F.data.startswith("mon:"))
async def open_month(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_month_card(cb, repo, config, cb.data.split(":", 1)[1])


# --------------------------------------------------------------- summary


@router.callback_query(F.data.startswith("mons:"))
async def month_summary(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    key = cb.data.split(":", 1)[1]
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    stats = await period_group_stats(repo, period)
    lines = [period_header(period), ""]
    if not stats:
        lines.append("Студентов нет.")
    for index, item in enumerate(stats, start=1):
        lines.append(f"{index}. {esc(item.student.full_name)} — <b>{percent_text(item.percent)}</b>")
    if len("\n".join(lines)) > 3900:
        kept: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > 3800:
                kept.append("… список обрезан, полный есть в отчёте Excel")
                break
            kept.append(line)
            size += len(line) + 1
        lines = kept
    text = "\n".join(lines)
    b = InlineKeyboardBuilder()
    b.row(btn("📑 Отчёт Excel", f"monx:{period.key}"))
    b.row(*nav_row(f"mon:{period.key}"))
    await respond(cb, text, b.as_markup())


# ---------------------------------------------------------------- report


@router.callback_query(F.data.startswith("monx:"))
async def month_report(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    key = cb.data.split(":", 1)[1]
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    if not period.events:
        await answer_callback(cb, "В периоде нет занятий — отчёт пуст.", show_alert=True)
        return
    stats = await period_group_stats(repo, period)
    data = build_report_xlsx(
        org_title=config.org_title,
        period_title=period.title,
        period_from=period.start_date,
        period_to=period.end_date,
        stats=stats,
        events=period.events,
        interim=not period.closed,
    )
    safe_title = re.sub(r"[^\w.-]+", "_", period.title.replace("Текущий: ", "")).strip("_") or "period"
    prefix = "Промежуточный_отчёт" if not period.closed else "Отчёт"
    await answer_callback(cb, "Готовлю файл…")
    await cb.message.answer_document(
        bytes_file(data, f"{prefix}_{safe_title}.xlsx"),
        caption=f"{'Промежуточный отчёт' if not period.closed else 'Отчёт'}: {esc(period.title)}\n"
        f"{period.start_date.strftime('%d.%m.%Y')} — {period.end_date.strftime('%d.%m.%Y')} · {len(stats)} студентов",
    )
    await audit(repo, actor, "report.export", entity_type="period", entity_id=period.closer.id if period.closer else None, период=period.title)


# ---------------------------------------------------- close / move closer


@router.callback_query(F.data.startswith("monc:"))
async def month_pick_closer(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    key = cb.data.split(":", 1)[1]
    periods = await load_periods(repo, config)
    index = period_index(periods, key)
    if index < 0:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    period = periods[index]
    candidates = list(period.events)
    if period.closed and index + 1 < len(periods):
        candidates += periods[index + 1].events
    if not candidates:
        await answer_callback(cb, "Нет занятий, которые можно сделать последними.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for event in candidates:
        mark = "🔚 " if event.closes_month else ""
        time = f" {event.start_time}" if event.start_time else ""
        b.row(btn(f"{mark}{event.day.strftime('%d.%m')}{time} · {event.title[:28]}", f"monce:{period.key}:{event.id}"))
    b.row(*nav_row(f"mon:{period.key}"))
    text = (
        f"{period_header(period)}\n\n"
        "Выберите занятие, которое будет <b>последним в этом месяце</b>. "
        "Все занятия после него перейдут в следующий месяц."
    )
    await respond(cb, text, b.as_markup())


@router.callback_query(F.data.startswith("monce:"))
async def month_set_closer(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    _, key, raw_event = cb.data.split(":", 2)
    event = await repo.get_event(int(raw_event))
    if event is None or event.deleted_at:
        await answer_callback(cb, "Занятие не найдено", show_alert=True)
        return
    periods = await load_periods(repo, config)
    index = period_index(periods, key)
    period = periods[index] if index >= 0 else None
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    allowed = {item.id for item in period.events}
    if index + 1 < len(periods):
        allowed.update(item.id for item in periods[index + 1].events)
    if event.id not in allowed:
        await answer_callback(cb, "Это занятие из другого месяца. Конец можно перенести только на занятие этого или следующего месяца.", show_alert=True)
        return
    if period.closer is not None and period.closer.id == event.id:
        await show_month_card(cb, repo, config, key, notice="Это занятие уже последнее")
        return
    custom_title = None
    if period.closer is not None:
        custom_title = period.closer.month_title
        await repo.update_event(period.closer.id, closes_month=0, month_title=None)
    await repo.update_event(event.id, closes_month=1, month_title=custom_title)
    action = "month.move" if period.closed else "month.close"
    await audit(
        repo,
        actor,
        action,
        entity_type="event",
        entity_id=event.id,
        месяц=period.title,
        последнее_занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}",
    )
    await show_month_card(cb, repo, config, str(event.id), notice="Готово")


# ---------------------------------------------------------------- rename


@router.callback_query(F.data.startswith("monr:"))
async def month_rename_start(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    key = cb.data.split(":", 1)[1]
    if key == OPEN_KEY:
        await answer_callback(cb, "Текущий месяц переименовать нельзя — сначала закройте его.", show_alert=True)
        return
    await state.set_state(MonthSG.rename)
    await state.update_data(rename_key=key)
    await respond(cb, "Новое название месяца? Например: <i>Сентябрь 2026</i>.", cancel_kb(f"mon:{key}"))


@router.message(MonthSG.rename, F.text)
async def month_rename(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    if not await require_level(message, actor):
        await state.clear()
        return
    title = " ".join((message.text or "").split())[:60]
    data = await state.get_data()
    key = str(data.get("rename_key"))
    await state.clear()
    if not title or not key.isdigit():
        await message.answer("Название не может быть пустым.")
        return
    event = await repo.get_event(int(key))
    if event is None or not event.closes_month:
        await message.answer("Период не найден.")
        return
    old = event.month_title
    await repo.update_event(event.id, month_title=title)
    await audit(repo, actor, "month.rename", entity_type="event", entity_id=event.id, было=old, стало=title)
    await show_month_card(message, repo, config, key)


# ---------------------------------------------------------------- reopen


@router.callback_query(F.data.startswith("mono:"))
async def month_reopen_ask(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    key = cb.data.split(":", 1)[1]
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None or not period.closed:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    await respond(
        cb,
        f"{period_header(period)}\n\nОткрыть месяц обратно? Его занятия объединятся со следующим периодом.",
        confirm_kb(f"monoc:{key}", f"mon:{key}", yes_text="❌ Открыть обратно"),
    )


@router.callback_query(F.data.startswith("monoc:"))
async def month_reopen(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    key = cb.data.split(":", 1)[1]
    event = await repo.get_event(int(key)) if key.isdigit() else None
    if event is None or not event.closes_month:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    title = event.month_title or ""
    await repo.update_event(event.id, closes_month=0, month_title=None)
    await audit(repo, actor, "month.reopen", entity_type="event", entity_id=event.id, месяц=title or None, занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}")
    await answer_callback(cb, "Месяц открыт")
    await show_months(cb, repo, config)
