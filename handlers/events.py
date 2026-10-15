"""Events: creation (from template or manual), list, card, marking screen, edits."""

from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.models import Event
from database.queries import Repo
from handlers.common import KIND_TITLES, event_title_line, load_periods, require_level, respond
from keyboards.main import (
    back_kb,
    calendar_kb,
    cancel_kb,
    confirm_kb,
    date_quick_kb,
    event_card_kb,
    events_list_kb,
    kind_kb,
    marking_kb,
    nav_row,
    pager_row,
    templates_pick_kb,
    time_kb,
)
from services.audit import audit, format_audit_entry
from services.periods import period_of_event, period_range_text
from services.roles import Actor
from states import EditEventSG, NewEventSG
from utils.callbacks import NAV_MAIN, SEC_EVENTS, SEC_NEW_EVENT
from utils.formatting import esc, short_name
from utils.telegram import answer_callback, safe_edit
from utils.time import local_now, local_today, parse_date_text, parse_hhmm

router = Router(name="events")

HISTORY_PAGE = 10


# ---------------------------------------------------------------- helpers


async def _creator_name(repo: Repo, telegram_id: int) -> str:
    student = await repo.get_student_by_telegram(telegram_id)
    if student is not None:
        return student.full_name
    user = await repo.get_user(telegram_id)
    return user.display if user else str(telegram_id)


async def event_card_text(repo: Repo, config: Config, event: Event) -> str:
    present = await repo.attendance_student_ids(event.id)
    students = await repo.list_students()
    eligible_ids = {s.id for s in students if s.counts_on(event.day)}
    periods = await load_periods(repo, config)
    period = period_of_event(periods, event.id)
    lines = [
        f"📘 <b>{esc(event.title)}</b>" if event.is_mandatory else f"🟢 <b>{esc(event.title)}</b>",
        f"Дата: {event.day.strftime('%d.%m.%Y')}" + (f" {event.start_time}" if event.start_time else ""),
        f"Тип: {KIND_TITLES[event.kind]}",
        f"Отмечено: <b>{len(present & eligible_ids)}</b> из {len(eligible_ids)}",
        f"Месяц: {esc(period.title) if period else '—'}" + (" · <b>последнее занятие месяца</b>" if event.closes_month else ""),
        f"Создал: {esc(await _creator_name(repo, event.created_by))}",
    ]
    return "\n".join(lines)


async def show_event_card(target: CallbackQuery | Message, repo: Repo, config: Config, event_id: int, *, notice: str | None = None) -> None:
    event = await repo.get_event(event_id)
    if event is None or event.deleted_at:
        if isinstance(target, CallbackQuery):
            await answer_callback(target, "Занятие не найдено или удалено", show_alert=True)
        else:
            await target.answer("Занятие не найдено или удалено")
        return
    text = await event_card_text(repo, config, event)
    markup = event_card_kb(event, back="evp:0")
    if isinstance(target, CallbackQuery):
        await answer_callback(target, notice)
        await safe_edit(target.message, text, markup)
    else:
        if notice:
            text = f"<b>{esc(notice)}</b>\n\n{text}"
        await target.answer(text, reply_markup=markup)


async def show_marking(target: CallbackQuery | Message, repo: Repo, event: Event, *, notice: str | None = None) -> None:
    students = await repo.list_students()
    present = await repo.attendance_student_ids(event.id)
    eligible = [s for s in students if s.counts_on(event.day)]
    skipped = len(students) - len(eligible)
    present_n = len(present & {s.id for s in eligible})
    text = (
        f"✅ <b>Отметка присутствия</b>\n{event_title_line(event)}\n\n"
        f"Отмечено: <b>{present_n}</b> из {len(eligible)}\n"
        "Нажмите на студента — появится галочка. Нажмите ещё раз, чтобы снять."
    )
    if skipped:
        text += (
            f"\n\nНе учитываются на эту дату: {skipped}. "
            "Кнопка с «·» — студент ещё не был в группе, отметка в процент не войдёт."
        )
    markup = marking_kb(event, students, present, back=f"ev:{event.id}")
    if isinstance(target, CallbackQuery):
        await answer_callback(target, notice)
        await safe_edit(target.message, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


# ------------------------------------------------------------- new event


@router.callback_query(F.data == SEC_NEW_EVENT)
async def new_event_start(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    templates = await repo.list_templates()
    text = "➕ <b>Новое занятие</b>\n\nВыберите шаблон или создайте занятие вручную:"
    if not templates:
        text += "\n\nШаблонов пока нет — их можно завести в разделе «Шаблоны»."
    await respond(cb, text, templates_pick_kb(templates, back=NAV_MAIN))


@router.callback_query(F.data.startswith("newt:"))
async def new_event_template(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    template_id = int(cb.data.split(":", 1)[1])
    payload: dict = {"tpl_id": None, "tpl_title": None, "tpl_kind": None, "tpl_time": None}
    if template_id:
        tpl = await repo.get_template(template_id)
        if tpl is None:
            await answer_callback(cb, "Шаблон не найден", show_alert=True)
            return
        payload = {"tpl_id": tpl.id, "tpl_title": tpl.title, "tpl_kind": tpl.kind, "tpl_time": tpl.default_time}
    await state.set_state(NewEventSG.date)
    await state.update_data(**payload)
    head = f"Шаблон: <b>{esc(payload['tpl_title'])}</b>\n\n" if payload["tpl_title"] else ""
    await respond(
        cb,
        f"{head}Дата занятия? Можно нажать кнопку или написать дату, например <code>12.09</code> или <code>12.09.2026</code>.",
        date_quick_kb("nd", local_today(config.timezone), back=SEC_NEW_EVENT),
    )


async def _ask_time(target: CallbackQuery | Message, state: FSMContext, day: date) -> None:
    data = await state.get_data()
    await state.set_state(NewEventSG.time)
    await state.update_data(ev_date=day.isoformat())
    await respond(
        target,
        f"Дата: <b>{day.strftime('%d.%m.%Y')}</b>\n\nВремя начала? Нажмите кнопку или напишите, например <code>10:30</code>.",
        time_kb("nt", default_time=data.get("tpl_time"), back=SEC_NEW_EVENT),
    )


@router.callback_query(NewEventSG.date, F.data.startswith("ndm:"))
async def new_event_calendar(cb: CallbackQuery) -> None:
    year, month = cb.data.split(":", 1)[1].split("-")
    await respond(cb, "Выберите дату:", calendar_kb(int(year), int(month), "nd", back=SEC_NEW_EVENT))


@router.callback_query(NewEventSG.date, F.data.startswith("nd:"))
async def new_event_date(cb: CallbackQuery, state: FSMContext) -> None:
    await _ask_time(cb, state, date.fromisoformat(cb.data.split(":", 1)[1]))


@router.message(NewEventSG.date, F.text)
async def new_event_date_text(message: Message, state: FSMContext, config: Config) -> None:
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        await message.answer("Не понял дату. Примеры: <code>12.09</code>, <code>12.09.2026</code>, <code>вчера</code>.", reply_markup=cancel_kb(SEC_NEW_EVENT))
        return
    await _ask_time(message, state, day)


async def _after_time(target: CallbackQuery | Message, state: FSMContext, repo: Repo, config: Config, actor: Actor, start_time: str | None) -> None:
    data = await state.get_data()
    await state.update_data(ev_time=start_time)
    if data.get("tpl_title"):
        await _create_event(target, state, repo, config, actor, title=data["tpl_title"], kind=data["tpl_kind"])
        return
    await state.set_state(NewEventSG.title)
    await respond(target, "Название занятия? Напишите текстом.", cancel_kb(SEC_NEW_EVENT))


@router.callback_query(NewEventSG.time, F.data.startswith("nt:"))
async def new_event_time(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    token = cb.data.split(":", 1)[1]
    if token == "now":
        start_time: str | None = local_now(config.timezone).strftime("%H:%M")
    elif token == "none":
        start_time = None
    else:
        start_time = token
    await _after_time(cb, state, repo, config, actor, start_time)


@router.message(NewEventSG.time, F.text)
async def new_event_time_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    try:
        start_time = parse_hhmm(message.text or "")
    except ValueError:
        await message.answer("Не понял время. Пример: <code>10:30</code>.", reply_markup=cancel_kb(SEC_NEW_EVENT))
        return
    await _after_time(message, state, repo, config, actor, start_time)


@router.message(NewEventSG.title, F.text)
async def new_event_title(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())[:80]
    if not title:
        await message.answer("Название не может быть пустым.", reply_markup=cancel_kb(SEC_NEW_EVENT))
        return
    await state.update_data(ev_title=title)
    await state.set_state(NewEventSG.kind)
    await message.answer(
        f"Название: <b>{esc(title)}</b>\n\nТип занятия?\n"
        "Обязательное входит в знаменатель процента; свободное учитывается только тем, кто пришёл.",
        reply_markup=kind_kb("nk", back=SEC_NEW_EVENT),
    )


@router.callback_query(NewEventSG.kind, F.data.startswith("nk:"))
async def new_event_kind(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    kind = cb.data.split(":", 1)[1]
    data = await state.get_data()
    await _create_event(cb, state, repo, config, actor, title=data["ev_title"], kind=kind)


async def _create_event(target: CallbackQuery | Message, state: FSMContext, repo: Repo, config: Config, actor: Actor, *, title: str, kind: str) -> None:
    if not await require_level(target, actor):
        await state.clear()
        return
    data = await state.get_data()
    event = await repo.create_event(
        title=title,
        kind=kind,
        event_date=data["ev_date"],
        start_time=data.get("ev_time"),
        template_id=data.get("tpl_id"),
        created_by=actor.telegram_id,
    )
    await state.clear()
    await audit(
        repo,
        actor,
        "event.create",
        entity_type="event",
        entity_id=event.id,
        занятие=title,
        дата=event.day.strftime("%d.%m.%Y"),
        время=event.start_time,
        тип=KIND_TITLES[kind],
    )
    await show_marking(target, repo, event, notice="Занятие создано")


# ------------------------------------------------------------- marking


@router.callback_query(F.data.startswith("mark:"))
async def open_marking(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    event = await repo.get_event(int(cb.data.split(":", 1)[1]))
    if event is None or event.deleted_at:
        await answer_callback(cb, "Занятие не найдено", show_alert=True)
        return
    await show_marking(cb, repo, event)


@router.callback_query(F.data.startswith("att:"))
async def toggle_attendance(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    _, raw_event, raw_student = cb.data.split(":", 2)
    event = await repo.get_event(int(raw_event))
    student = await repo.get_student(int(raw_student))
    if event is None or event.deleted_at or student is None:
        await answer_callback(cb, "Занятие или студент не найдены", show_alert=True)
        return
    present_now = await repo.is_present(event.id, student.id)
    if not student.counts_on(event.day) and not present_now:
        await answer_callback(
            cb,
            f"{student.full_name} не учитывается на {event.day.strftime('%d.%m.%Y')}: "
            "на эту дату студента ещё не было в группе.",
            show_alert=True,
        )
        return
    changed = await repo.set_presence(event.id, student.id, not present_now, actor.telegram_id)
    if changed:
        await audit(
            repo,
            actor,
            "attendance.unmark" if present_now else "attendance.mark",
            entity_type="event",
            entity_id=event.id,
            студент=student.full_name,
            занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}",
        )
    await show_marking(cb, repo, event, notice=f"{'Снято' if present_now else 'Отмечено'}: {short_name(student.full_name)}")


# ---------------------------------------------------------------- list


async def show_events_list(target: CallbackQuery | Message, repo: Repo, page: int = 0) -> None:
    events = list(reversed(await repo.list_events()))
    students = await repo.list_students()
    attendance = await repo.attendance_all()
    counts = {
        event.id: len(attendance.get(event.id, set()) & {s.id for s in students if s.counts_on(event.day)})
        for event in events
    }
    text = "📅 <b>Занятия</b>\n\nДата · название · отмечено. 🔚 — последнее занятие месяца."
    if not events:
        text = "📅 <b>Занятия</b>\n\nЗанятий пока нет. Создайте первое через «Новое занятие»."
    await respond(target, text, events_list_kb(events, counts, page))


@router.callback_query(F.data == SEC_EVENTS)
async def open_events(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_events_list(cb, repo)


@router.callback_query(F.data.startswith("evp:"))
async def events_page(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_events_list(cb, repo, int(cb.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("ev:"))
async def open_event(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_event_card(cb, repo, config, int(cb.data.split(":", 1)[1]))


# ---------------------------------------------------------------- edits


@router.callback_query(F.data.startswith("eve:"))
async def edit_event_start(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    _, field, raw_id = cb.data.split(":", 2)
    event = await repo.get_event(int(raw_id))
    if event is None or event.deleted_at:
        await answer_callback(cb, "Занятие не найдено", show_alert=True)
        return
    back = f"ev:{event.id}"
    if field == "kind":
        new_kind = "free" if event.is_mandatory else "mandatory"
        await repo.update_event(event.id, kind=new_kind)
        await audit(repo, actor, "event.update", entity_type="event", entity_id=event.id, поле="тип", было=KIND_TITLES[event.kind], стало=KIND_TITLES[new_kind])
        await show_event_card(cb, repo, config, event.id, notice="Тип изменён")
        return
    await state.update_data(edit_event_id=event.id)
    if field == "title":
        await state.set_state(EditEventSG.title)
        await respond(cb, f"Текущее название: <b>{esc(event.title)}</b>\n\nНовое название?", cancel_kb(back))
    elif field == "date":
        await state.set_state(EditEventSG.date)
        await respond(
            cb,
            f"Текущая дата: <b>{event.day.strftime('%d.%m.%Y')}</b>\n\n"
            "Новая дата? Кнопка или текст. От даты зависит, в какой месяц попадёт занятие."
            + (" Это последнее занятие месяца — граница переедет вместе с ним." if event.closes_month else ""),
            date_quick_kb("ed", local_today(config.timezone), back=back),
        )
    elif field == "time":
        await state.set_state(EditEventSG.time)
        await respond(
            cb,
            f"Текущее время: <b>{event.start_time or 'не задано'}</b>\n\nНовое время? Кнопка или текст."
            + (" Это последнее занятие месяца: от времени зависит, какие занятия этого дня останутся в месяце." if event.closes_month else ""),
            time_kb("et", back=back),
        )


async def _apply_edit(target: CallbackQuery | Message, state: FSMContext, repo: Repo, config: Config, actor: Actor, **fields) -> None:
    if not await require_level(target, actor):
        await state.clear()
        return
    data = await state.get_data()
    event_id = data.get("edit_event_id")
    event = await repo.get_event(int(event_id)) if event_id else None
    if event is None:
        await state.clear()
        await respond(target, "Занятие не найдено.", back_kb(SEC_EVENTS))
        return
    labels = {"title": "название", "event_date": "дата", "start_time": "время"}
    before = {k: getattr(event, k) for k in fields}
    moved_period = "event_date" in fields or "start_time" in fields
    before_period = period_of_event(await load_periods(repo, config), event.id) if moved_period else None
    await repo.update_event(event.id, **fields)
    await state.clear()
    for key, value in fields.items():
        await audit(repo, actor, "event.update", entity_type="event", entity_id=event.id, поле=labels[key], было=before[key], стало=value)
    notice = "Сохранено"
    if moved_period:
        after_period = period_of_event(await load_periods(repo, config), event.id)
        before_ids = {item.id for item in before_period.events} if before_period else set()
        after_ids = {item.id for item in after_period.events} if after_period else set()
        if event.closes_month and before_ids != after_ids:
            notice = "Сохранено. Граница месяца переехала вместе с занятием."
        elif before_period and after_period and before_period.key != after_period.key:
            notice = f"Сохранено. Занятие теперь в месяце «{after_period.title}» ({period_range_text(after_period)})."
    await show_event_card(target, repo, config, event.id, notice=notice)


@router.message(EditEventSG.title, F.text)
async def edit_title(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    title = " ".join((message.text or "").split())[:80]
    if not title:
        await message.answer("Название не может быть пустым.")
        return
    await _apply_edit(message, state, repo, config, actor, title=title)


@router.callback_query(EditEventSG.date, F.data.startswith("edm:"))
async def edit_date_calendar(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    year, month = cb.data.split(":", 1)[1].split("-")
    await respond(cb, "Выберите дату:", calendar_kb(int(year), int(month), "ed", back=f"ev:{data.get('edit_event_id')}"))


@router.callback_query(EditEventSG.date, F.data.startswith("ed:"))
async def edit_date(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    await _apply_edit(cb, state, repo, config, actor, event_date=cb.data.split(":", 1)[1])


@router.message(EditEventSG.date, F.text)
async def edit_date_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        await message.answer("Не понял дату. Примеры: <code>12.09</code>, <code>12.09.2026</code>.")
        return
    await _apply_edit(message, state, repo, config, actor, event_date=day.isoformat())


@router.callback_query(EditEventSG.time, F.data.startswith("et:"))
async def edit_time(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    token = cb.data.split(":", 1)[1]
    if token == "now":
        value: str | None = local_now(config.timezone).strftime("%H:%M")
    elif token == "none":
        value = None
    else:
        value = token
    await _apply_edit(cb, state, repo, config, actor, start_time=value)


@router.message(EditEventSG.time, F.text)
async def edit_time_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    try:
        value = parse_hhmm(message.text or "")
    except ValueError:
        await message.answer("Не понял время. Пример: <code>10:30</code>.")
        return
    await _apply_edit(message, state, repo, config, actor, start_time=value)


# --------------------------------------------------------- month closer


@router.callback_query(F.data.startswith("evc:"))
async def toggle_closer(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    _, mode, raw_id = cb.data.split(":", 2)
    event = await repo.get_event(int(raw_id))
    if event is None or event.deleted_at:
        await answer_callback(cb, "Занятие не найдено", show_alert=True)
        return
    if mode == "on":
        periods = await load_periods(repo, config)
        period = period_of_event(periods, event.id)
        moved = period is not None and period.closer is not None and period.closer.id != event.id
        kept_title = period.closer.month_title if moved and period and period.closer else None
        if moved and period and period.closer:
            await repo.update_event(period.closer.id, closes_month=0, month_title=None)
        await repo.update_event(event.id, closes_month=1, month_title=kept_title)
        await audit(
            repo,
            actor,
            "month.move" if moved else "month.close",
            entity_type="event",
            entity_id=event.id,
            последнее_занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}",
        )
        notice = "Конец месяца перенесён" if moved else "Теперь это последнее занятие месяца"
    else:
        await repo.update_event(event.id, closes_month=0, month_title=None)
        await audit(repo, actor, "month.reopen", entity_type="event", entity_id=event.id, занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}")
        notice = "Месяц открыт обратно"
    await show_event_card(cb, repo, config, event.id, notice=notice)


# ---------------------------------------------------------------- delete


@router.callback_query(F.data.startswith("evd:"))
async def delete_event_ask(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    event = await repo.get_event(int(cb.data.split(":", 1)[1]))
    if event is None or event.deleted_at:
        await answer_callback(cb, "Занятие не найдено", show_alert=True)
        return
    present = len(await repo.attendance_student_ids(event.id))
    boundary = ""
    if event.closes_month:
        periods = await load_periods(repo, config)
        period = period_of_event(periods, event.id)
        if period is not None and len(period.events) > 1:
            boundary = "\n\nЭто последнее занятие месяца. Граница перейдёт на предыдущее занятие, месяц не сольётся со следующим."
        else:
            boundary = "\n\nЭто единственное занятие закрытого месяца. После удаления месяц откроется и сольётся со следующим."
    await respond(
        cb,
        f"Удалить занятие?\n{event_title_line(event)}\nОтметок присутствия: {present}\n\n"
        "Занятие исчезнет из статистики. Запись о нём останется в журнале."
        f"{boundary}",
        confirm_kb(f"evdc:{event.id}", f"ev:{event.id}", yes_text="🗑 Удалить"),
    )


@router.callback_query(F.data.startswith("evdc:"))
async def delete_event(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    event = await repo.get_event(int(cb.data.split(":", 1)[1]))
    if event is None or event.deleted_at:
        await answer_callback(cb, "Занятие не найдено", show_alert=True)
        return
    if event.closes_month:
        periods = await load_periods(repo, config)
        period = period_of_event(periods, event.id)
        if period is not None and len(period.events) > 1:
            previous = period.events[-2]
            await repo.update_event(previous.id, closes_month=1, month_title=event.month_title)
    await repo.delete_event(event.id)
    await audit(repo, actor, "event.delete", entity_type="event", entity_id=event.id, занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}")
    await answer_callback(cb, "Занятие удалено")
    await show_events_list(cb, repo)


# --------------------------------------------------------------- history


@router.callback_query(F.data.startswith("evh:"))
async def event_history(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    _, raw_id, raw_page = cb.data.split(":", 2)
    event_id, page = int(raw_id), int(raw_page)
    event = await repo.get_event(event_id)
    if event is None:
        await answer_callback(cb, "Занятие не найдено", show_alert=True)
        return
    total = await repo.count_audit(entity_type="event", entity_id=event_id)
    pages = max(1, (total + HISTORY_PAGE - 1) // HISTORY_PAGE)
    page = max(0, min(page, pages - 1))
    entries = await repo.list_audit(limit=HISTORY_PAGE, offset=page * HISTORY_PAGE, entity_type="event", entity_id=event_id)
    body = [format_audit_entry(e, config.timezone) for e in entries] or ["Записей нет."]
    text = f"📜 <b>История</b>\n{event_title_line(event)}\n\n" + "\n\n".join(body)
    b = InlineKeyboardBuilder()
    if pages > 1:
        b.row(*pager_row(f"evh:{event_id}", page, pages))
    b.row(*nav_row(f"ev:{event_id}"))
    await respond(cb, text, b.as_markup())
