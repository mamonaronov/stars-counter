"""Reporting periods: close a month, move its end, rename, reopen, Excel report."""

from __future__ import annotations

import re
from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.queries import MonthBoundaryError, Repo
from handlers.common import event_title_line, load_periods, period_header, require_level, respond
from keyboards.main import btn, calendar_kb, cancel_kb, confirm_kb, date_quick_kb, nav_row, periods_kb
from services.audit import audit
from services.periods import (
    OPEN_KEY,
    Period,
    ensure_scheduled_month_end,
    find_period,
    month_end_date_is_taken,
    period_index,
    previous_closed_period,
    scheduled_end_is_pending,
    scheduled_month_end,
    drop_scheduled_month_end_unless,
    set_scheduled_month_end,
    students_for_period,
)
from services.report import build_report_xlsx
from services.roles import LEVEL_NONE, Actor
from services.stats import StudentStats, compute_group_stats
from states import MonthSG
from utils.callbacks import SEC_MONTHS, SEC_REPORT_PREV, SEC_REPORTS
from utils.formatting import esc, percent_text
from utils.telegram import answer_callback, bytes_file
from utils.time import format_date, local_today, parse_date_text

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
        "Дату конца можно назначить заранее: последнее занятие этого дня закроет месяц. "
        "Следующее занятие автоматически открывает новый месяц."
    )
    await respond(target, text, periods_kb(periods, "mon"))


async def show_month_card(
    target: CallbackQuery | Message,
    repo: Repo,
    config: Config,
    key: str,
    *,
    notice: str | None = None,
    can_manage: bool = False,
) -> None:
    moved = await ensure_scheduled_month_end(repo)
    periods = await load_periods(repo, config)
    if moved is not None and (key == OPEN_KEY or find_period(periods, key) is None):
        opened_current = key == OPEN_KEY
        key = str(moved.id)
        if opened_current or notice is None:
            notice = "Месяц закрыт последним занятием этого дня"
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
    planned = await scheduled_month_end(repo)
    planned_pending = planned is not None and not period.closed and scheduled_end_is_pending(periods, planned)
    if planned_pending and planned is not None:
        lines.append(
            f"Конец месяца: <b>{format_date(planned)}</b>. "
            "Последнее занятие этого дня закроет месяц."
        )
    elif planned is not None and period.closer is not None and period.closer.day == planned:
        lines.append(f"Дата конца месяца: {format_date(planned)}")
    b = InlineKeyboardBuilder()
    b.row(btn("📑 Отчёт Excel", f"monx:{period.key}"), btn("📊 Сводка", f"mons:{period.key}"))
    if can_manage and period.closed:
        b.row(btn("🔁 Другое последнее занятие", f"monc:{period.key}"))
        b.row(btn("✏️ Переименовать", f"monr:{period.key}"), btn("❌ Открыть обратно", f"mono:{period.key}"))
    elif can_manage:
        if period.events:
            b.row(btn("🔚 Закрыть месяц", f"monc:{period.key}"))
        if planned_pending and planned is not None:
            b.row(btn(f"📅 Конец: {format_date(planned)}", "med"), btn("✖️ Убрать дату", "medx"))
        else:
            b.row(btn("📅 Назначить конец месяца", "med"))
    b.row(*nav_row(SEC_MONTHS))
    await respond(target, "\n".join(lines), b.as_markup(), notice=notice)


# ------------------------------------------------------------------ list


@router.callback_query(F.data == SEC_MONTHS)
async def open_months(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_NONE):
        return
    await state.clear()
    await show_months(cb, repo, config)


@router.callback_query(F.data.startswith("mon:"))
async def open_month(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_NONE):
        return
    await state.clear()
    await show_month_card(cb, repo, config, cb.data.split(":", 1)[1], can_manage=bool(actor and actor.can_mark))


# --------------------------------------------------------------- summary


@router.callback_query(F.data.startswith("mons:"))
async def month_summary(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_NONE):
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


async def send_period_report(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor, period: Period) -> None:
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


@router.callback_query(F.data == SEC_REPORTS)
async def current_month_report(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_NONE):
        return
    assert actor is not None
    periods = await load_periods(repo, config)
    period = find_period(periods, OPEN_KEY)
    if period is None:
        await answer_callback(cb, "Текущий месяц не найден", show_alert=True)
        return
    await send_period_report(cb, repo, config, actor, period)


@router.callback_query(F.data == SEC_REPORT_PREV)
async def previous_month_report(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_NONE):
        return
    assert actor is not None
    periods = await load_periods(repo, config)
    period = previous_closed_period(periods)
    if period is None:
        await answer_callback(cb, "Прошлого месяца ещё нет.", show_alert=True)
        return
    await send_period_report(cb, repo, config, actor, period)


@router.callback_query(F.data.startswith("monx:"))
async def month_report(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_NONE):
        return
    assert actor is not None
    key = cb.data.split(":", 1)[1]
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    await send_period_report(cb, repo, config, actor, period)


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
    custom_title = period.closer.month_title if period.closer is not None else None
    try:
        await repo.set_month_closer(
            event.id,
            previous_closer_id=period.closer.id if period.closer is not None else None,
            month_title=custom_title,
        )
    except MonthBoundaryError:
        await answer_callback(cb, "Граница месяца уже изменилась. Откройте месяц ещё раз.", show_alert=True)
        return
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
    await drop_scheduled_month_end_unless(repo, event.day)
    await show_month_card(cb, repo, config, str(event.id), notice="Готово", can_manage=True)


# --------------------------------------------------------------- scheduled end


def _month_end_back(key: str) -> str:
    return f"mon:{key}"


async def _ask_month_end(target: CallbackQuery | Message, config: Config, *, back: str, planned: date | None) -> None:
    when = f"Сейчас назначено: <b>{format_date(planned)}</b>.\n\n" if planned else ""
    await respond(
        target,
        f"{when}Когда закончится этот месяц? Выберите дату в календаре или напишите, "
        "например <code>20.12.2026</code>.\n\n"
        "Последнее занятие этого дня станет концом месяца. Если занятия ещё нет, дата сохранится и сработает, когда оно появится.",
        date_quick_kb("med", local_today(config.timezone), back=back),
    )


@router.callback_query(F.data == "med")
async def month_end_start(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    periods = await load_periods(repo, config)
    period = find_period(periods, OPEN_KEY)
    if period is None:
        await answer_callback(cb, "Текущий месяц не найден", show_alert=True)
        return
    planned = await scheduled_month_end(repo)
    if planned is not None and not scheduled_end_is_pending(periods, planned):
        planned = None
    await state.set_state(MonthSG.ends_on)
    await state.update_data(month_end_back=_month_end_back(period.key))
    await _ask_month_end(cb, config, back=_month_end_back(period.key), planned=planned)


@router.callback_query(MonthSG.ends_on, F.data.startswith("medm:"))
async def month_end_calendar(cb: CallbackQuery, state: FSMContext) -> None:
    year, month = cb.data.split(":", 1)[1].split("-")
    data = await state.get_data()
    await respond(
        cb,
        "Выберите дату конца месяца:",
        calendar_kb(int(year), int(month), "med", back=data.get("month_end_back")),
    )


async def _save_month_end(
    target: CallbackQuery | Message,
    state: FSMContext,
    repo: Repo,
    config: Config,
    actor: Actor,
    day: date,
) -> None:
    periods = await load_periods(repo, config)
    if month_end_date_is_taken(periods, day):
        if isinstance(target, CallbackQuery):
            await answer_callback(target, "Эта дата уже в закрытом месяце.", show_alert=True)
        else:
            await target.answer("Эта дата уже в закрытом месяце. Выберите день после него.")
        return
    await set_scheduled_month_end(repo, day)
    await state.clear()
    await audit(repo, actor, "month.schedule", дата=format_date(day))
    await show_month_card(
        target,
        repo,
        config,
        OPEN_KEY,
        notice="Дата конца месяца сохранена",
        can_manage=True,
    )


@router.callback_query(MonthSG.ends_on, F.data.startswith("med:"))
async def month_end_pick(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    if not await require_level(cb, actor):
        await state.clear()
        return
    try:
        day = date.fromisoformat(cb.data.split(":", 1)[1])
    except ValueError:
        await answer_callback(cb, "Некорректная дата", show_alert=True)
        return
    await _save_month_end(cb, state, repo, config, actor, day)


@router.message(MonthSG.ends_on, F.text)
async def month_end_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    if not await require_level(message, actor):
        await state.clear()
        return
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        data = await state.get_data()
        await message.answer(
            "Не понял дату. Примеры: <code>20.12</code>, <code>20.12.2026</code>.",
            reply_markup=cancel_kb(data.get("month_end_back")),
        )
        return
    await _save_month_end(message, state, repo, config, actor, day)


@router.callback_query(F.data == "medx")
async def month_end_clear(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    await state.clear()
    planned = await scheduled_month_end(repo)
    await set_scheduled_month_end(repo, None)
    if planned is not None:
        await audit(repo, actor, "month.unschedule", дата=format_date(planned))
    await show_month_card(cb, repo, config, OPEN_KEY, notice="Дата конца месяца убрана", can_manage=True)


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
    await show_month_card(message, repo, config, key, can_manage=True)


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
    await set_scheduled_month_end(repo, None)
    await repo.update_event(event.id, closes_month=0, month_title=None)
    await audit(repo, actor, "month.reopen", entity_type="event", entity_id=event.id, месяц=title or None, занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}")
    await answer_callback(cb, "Месяц открыт")
    await show_months(cb, repo, config)
