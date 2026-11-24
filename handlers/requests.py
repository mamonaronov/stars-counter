"""Student requests: "you forgot to mark me" — created by students, handled by markers."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.models import Event, Request
from database.queries import Repo
from handlers.common import event_title_line, load_periods, require_level, require_verified, respond, show_main
from keyboards.main import btn, nav_row, pager_row
from services.audit import audit
from services.periods import find_period, period_button_label
from services.roles import Actor
from states import RejectSG, RequestSG
from utils.callbacks import SEC_MY_REQUESTS, SEC_NEW_REQUEST, SEC_REQUESTS
from utils.formatting import esc, truncate
from utils.telegram import answer_callback, safe_send_message
from utils.time import format_dt_iso

router = Router(name="requests")

STATUS_TITLES = {"open": "ожидает", "approved": "одобрен", "rejected": "отклонён", "cancelled": "отменён"}
PERIOD_PAGE = 6
EVENT_PAGE = 8
REQUEST_PAGE = 8


def _paged(items: list, page: int, size: int) -> tuple[list, int, int]:
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    return items[page * size : (page + 1) * size], page, pages


def _stored_ids(data: dict) -> set[int]:
    raw = data.get("req_ids") or []
    found: set[int] = set()
    if not isinstance(raw, list):
        return found
    for item in raw:
        if isinstance(item, int):
            found.add(item)
        elif isinstance(item, str) and item.isdigit():
            found.add(int(item))
    return found


def _requests_word(count: int) -> str:
    n = abs(count) % 100
    if 11 <= n <= 14:
        return "запросов"
    last = n % 10
    if last == 1:
        return "запрос"
    if 2 <= last <= 4:
        return "запроса"
    return "запросов"


def _tab(text: str, on: bool) -> str:
    return f"[{text}]" if on else text


# ------------------------------------------------------------ student side


@router.callback_query(F.data == SEC_NEW_REQUEST)
async def new_request_start(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    await state.clear()
    periods = await load_periods(repo, config)
    await _show_request_periods(cb, periods, 0)


async def _show_request_periods(cb: CallbackQuery, periods, page: int) -> None:
    ordered = list(reversed(periods))
    chunk, page, pages = _paged(ordered, page, PERIOD_PAGE)
    b = InlineKeyboardBuilder()
    for period in chunk:
        b.row(btn(period_button_label(period), f"rqp:{period.key}"))
    if pages > 1:
        b.row(*pager_row("rqpg", page, pages))
    b.row(btn("📂 Мои запросы", "rqmy"))
    b.row(*nav_row())
    await respond(
        cb,
        "✉️ <b>Запрос отмечающему</b>\n\n"
        "За какой месяц занятия, на которых вас забыли отметить? Можно выбрать несколько.",
        b.as_markup(),
    )


@router.callback_query(F.data.startswith("rqpg:"))
async def request_periods_page(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    periods = await load_periods(repo, config)
    await _show_request_periods(cb, periods, int(cb.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("rqp:"))
async def new_request_period(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    key = cb.data.split(":", 1)[1]
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    await _show_request_events(cb, state, repo, actor, period, 0, set())


async def _period_choices(repo: Repo, actor: Actor, period) -> tuple[list[Event], set[int], set[int]]:
    """Unmarked events (newest first), ids that can be requested, ids that already have an open request."""
    assert actor.student is not None
    present = await repo.attendance_of_student(actor.student.id)
    pending = await repo.open_request_event_ids(actor.student.id)
    visible = [event for event in reversed(period.events) if event.id not in present and actor.student.counts_on(event.day)]
    pending_ids = {event.id for event in visible if event.id in pending}
    selectable = {event.id for event in visible if event.id not in pending_ids}
    return visible, selectable, pending_ids


def _event_choice_label(event: Event, *, selected: bool, pending: bool) -> str:
    time = f" {event.start_time}" if event.start_time else ""
    kind = "" if event.is_mandatory else " (св.)"
    prefix = "⏳ " if pending else ("✅ " if selected else "")
    return f"{prefix}{event.day.strftime('%d.%m')}{time} · {truncate(event.title, 24)}{kind}"


async def _show_request_events(
    cb: CallbackQuery,
    state: FSMContext,
    repo: Repo,
    actor: Actor,
    period,
    page: int,
    selected: set[int],
) -> None:
    visible, selectable, pending_ids = await _period_choices(repo, actor, period)
    if not visible:
        await state.clear()
        await answer_callback(cb, "В этом месяце нет занятий, где вы не отмечены.", show_alert=True)
        return
    chosen = selected & selectable
    chunk, page, pages = _paged(visible, page, EVENT_PAGE)
    await state.set_state(RequestSG.events)
    await state.update_data(req_period=period.key, req_page=page, req_ids=sorted(chosen))
    b = InlineKeyboardBuilder()
    for event in chunk:
        pending = event.id in pending_ids
        callback = f"rqw:{event.id}" if pending else f"rqt:{event.id}"
        b.row(btn(_event_choice_label(event, selected=event.id in chosen, pending=pending), callback))
    if pages > 1:
        b.row(*pager_row(f"rqel:{period.key}", page, pages))
    actions = []
    if selectable and chosen != selectable:
        actions.append(btn("Выбрать все", "rqall"))
    if chosen:
        actions.append(btn("Снять все", "rqclr"))
    if actions:
        b.row(*actions)
    further = f"Далее · {len(chosen)}" if chosen else "Далее"
    b.row(btn(further, "rqok"))
    b.row(btn("📂 Мои запросы", "rqmy"))
    b.row(*nav_row(SEC_NEW_REQUEST))
    lines = [
        f"📅 <b>{esc(period.title)}</b>",
        "",
        "Отметьте занятия, где вы не отмечены. Можно выбрать несколько, затем нажмите «Далее».",
    ]
    if pending_ids:
        lines.append("⏳ — по занятию уже есть необработанный запрос.")
    if not selectable:
        lines.append("По всем таким занятиям запрос уже отправлен. Статус — в «Мои запросы».")
    await respond(cb, "\n".join(lines), b.as_markup())


async def _period_from_state(state: FSMContext, repo: Repo, config: Config):
    data = await state.get_data()
    key = data.get("req_period")
    if not isinstance(key, str) or not key:
        return None, data
    return find_period(await load_periods(repo, config), key), data


def _no_period_text(data: dict) -> str:
    return "Период не найден." if data.get("req_period") else "Сначала выберите месяц."


@router.callback_query(F.data.startswith("rqel:"))
async def request_events_page(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None
    _, key, raw_page = cb.data.split(":", 2)
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    data = await state.get_data()
    selected = _stored_ids(data) if data.get("req_period") == period.key else set()
    await _show_request_events(cb, state, repo, actor, period, int(raw_page), selected)


@router.callback_query(F.data.startswith("rqt:"))
async def toggle_request_event(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None
    period, data = await _period_from_state(state, repo, config)
    if period is None:
        await answer_callback(cb, _no_period_text(data), show_alert=True)
        return
    event_id = int(cb.data.split(":", 1)[1])
    _, selectable, pending_ids = await _period_choices(repo, actor, period)
    if event_id in pending_ids:
        await answer_callback(cb, "По этому занятию уже есть необработанный запрос.", show_alert=True)
        return
    if event_id not in selectable:
        await answer_callback(cb, "Это занятие уже нельзя запросить.", show_alert=True)
        return
    selected = _stored_ids(data)
    if event_id in selected:
        selected.discard(event_id)
    else:
        selected.add(event_id)
    await _show_request_events(cb, state, repo, actor, period, int(data.get("req_page") or 0), selected)


@router.callback_query(F.data.startswith("rqw:"))
async def pending_request_event(cb: CallbackQuery, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    await answer_callback(cb, "По этому занятию уже есть необработанный запрос.", show_alert=True)


@router.callback_query(F.data == "rqall")
async def select_all_request_events(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None
    period, data = await _period_from_state(state, repo, config)
    if period is None:
        await answer_callback(cb, _no_period_text(data), show_alert=True)
        return
    _, selectable, _pending = await _period_choices(repo, actor, period)
    if not selectable:
        await answer_callback(cb, "Нет занятий, по которым можно отправить запрос.", show_alert=True)
        return
    await _show_request_events(cb, state, repo, actor, period, int(data.get("req_page") or 0), selectable)


@router.callback_query(F.data == "rqclr")
async def clear_request_events(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None
    period, data = await _period_from_state(state, repo, config)
    if period is None:
        await answer_callback(cb, _no_period_text(data), show_alert=True)
        return
    await _show_request_events(cb, state, repo, actor, period, int(data.get("req_page") or 0), set())


@router.callback_query(F.data == "rqok")
async def request_events_done(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None
    period, data = await _period_from_state(state, repo, config)
    if period is None:
        await answer_callback(cb, _no_period_text(data), show_alert=True)
        return
    visible, selectable, _pending = await _period_choices(repo, actor, period)
    chosen = [event for event in visible if event.id in _stored_ids(data) and event.id in selectable]
    chosen.sort(key=lambda event: event.sort_key)
    if not chosen:
        await answer_callback(cb, "Выберите хотя бы одно занятие.", show_alert=True)
        return
    await state.set_state(RequestSG.text)
    await state.update_data(req_ids=[event.id for event in chosen])
    lines = ["✉️ <b>Сообщение отмечающему</b>", "", "Занятия:"]
    lines.extend(f"• {event_title_line(event)}" for event in chosen)
    lines.append("")
    lines.append("Напишите, почему вас нужно отметить (например, «был, сидел на третьем ряду»). Одно сообщение уйдёт по всем выбранным занятиям.")
    b = InlineKeyboardBuilder()
    b.row(*nav_row("rqback"))
    await respond(cb, "\n".join(lines), b.as_markup())


@router.callback_query(F.data == "rqback")
async def request_events_back(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None
    period, data = await _period_from_state(state, repo, config)
    if period is None:
        periods = await load_periods(repo, config)
        await state.clear()
        await _show_request_periods(cb, periods, 0)
        return
    await _show_request_events(cb, state, repo, actor, period, int(data.get("req_page") or 0), _stored_ids(data))


def _sent_text(sent: int, skipped: int) -> str:
    if sent == 1:
        text = "✅ Запрос отправлен. Вам придёт сообщение, когда отмечающий его рассмотрит."
    else:
        text = f"✅ Отправлено {sent} {_requests_word(sent)}. Вам придёт сообщение, когда отмечающий их рассмотрит."
    text += " Статус — в «Мои запросы»."
    if skipped:
        text += " Некоторые занятия не вошли в запрос: вас уже отметили или необработанный запрос уже есть."
    return text


def _notify_text(student_name: str, text: str, items: list[tuple[Request, Event]]) -> str:
    lines = [f"📨 <b>Новый запрос</b> от {esc(student_name)}"]
    if len(items) == 1:
        lines.append(event_title_line(items[0][1]))
    else:
        lines.append(f"Занятий: {len(items)}")
        lines.append("")
        for _req, event in items:
            lines.append(f"• {event_title_line(event)}")
    lines.append("")
    lines.append(f"«{esc(text)}»")
    return "\n".join(lines)


@router.message(RequestSG.text, F.text)
async def new_request_text(message: Message, state: FSMContext, repo: Repo, config: Config, bot: Bot, actor: Actor) -> None:
    text = " ".join((message.text or "").split())[:500]
    if not text:
        await message.answer("Сообщение не может быть пустым.")
        return
    data = await state.get_data()
    await state.clear()
    if actor.student is None:
        await message.answer("Сначала выберите занятия.")
        await show_main(message, actor, config, repo)
        return
    event_ids = _stored_ids(data)
    events = [await repo.get_event(event_id) for event_id in sorted(event_ids)]
    events = [event for event in events if event is not None]
    events.sort(key=lambda event: event.sort_key)
    if not events:
        await message.answer("Сначала выберите занятия.")
        await show_main(message, actor, config, repo)
        return
    sent: list[tuple[Request, Event]] = []
    skipped = 0
    for event in events:
        if event.deleted_at or not actor.student.counts_on(event.day) or await repo.is_present(event.id, actor.student.id):
            skipped += 1
            continue
        req = await repo.create_request(actor.student.id, event.id, text)
        if req is None:
            skipped += 1
            continue
        sent.append((req, event))
        await audit(
            repo,
            actor,
            "request.create",
            entity_type="request",
            entity_id=req.id,
            занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}",
            текст=text,
        )
    if not sent:
        await message.answer("Запрос не отправлен: занятия уже отмечены или по ним уже есть необработанный запрос.")
        await show_main(message, actor, config, repo)
        return
    grouped: dict[int, list[tuple[Request, Event]]] = {}
    for item in sent:
        grouped.setdefault(item[1].created_by, []).append(item)
    for marker_id, items in grouped.items():
        markup = _request_actions_kb(items[0][0].id) if len(items) == 1 else _requests_inbox_kb()
        await safe_send_message(bot, marker_id, _notify_text(actor.student.full_name, text, items), reply_markup=markup)
    await message.answer(_sent_text(len(sent), skipped))
    await show_main(message, actor, config, repo)


# ------------------------------------------------------- student's own requests


def _list_back(data: dict) -> str | None:
    """Where «Назад» returns when the list was opened from the request wizard."""
    if data.get("req_from") != "wizard":
        return None
    return "rqback" if data.get("req_period") else SEC_NEW_REQUEST


async def show_my_requests(
    target: CallbackQuery | Message,
    repo: Repo,
    student_id: int,
    *,
    closed: bool = False,
    page: int = 0,
    back: str | None = None,
) -> None:
    open_items = await repo.list_requests(status="open", student_id=student_id)
    closed_items = await repo.list_requests(status=None, closed_only=True, student_id=student_id)
    items = closed_items if closed else open_items
    view = "c" if closed else "o"
    chunk, page, pages = _paged(items, page, REQUEST_PAGE)
    b = InlineKeyboardBuilder()
    b.row(
        btn(_tab(f"Открытые ({len(open_items)})", not closed), "mrqv:o"),
        btn(_tab(f"Закрытые ({len(closed_items)})", closed), "mrqv:c"),
    )
    for req in chunk:
        event = await repo.get_event(req.event_id)
        ev = f"{event.day.strftime('%d.%m')} {truncate(event.title, 24)}" if event else "?"
        icon = {"approved": "✅", "rejected": "❌", "cancelled": "↩️"}.get(req.status, "•")
        b.row(btn(f"{icon} {ev}", f"mrqi:{req.id}"))
    if pages > 1:
        b.row(*pager_row(f"mrqp:{view}", page, pages))
    b.row(btn("✉️ Новый запрос", SEC_NEW_REQUEST))
    b.row(*nav_row(back))
    title = "Закрытые" if closed else "Открытые"
    hint = (
        "Одобренные, отклонённые и отменённые. Нажмите, чтобы открыть."
        if closed
        else "Ожидают решения отмечающего. Нажмите, чтобы открыть или отменить."
    )
    text = f"📂 <b>Мои запросы</b>\n\n<b>{title}</b>\n{hint}"
    if not items:
        empty = "Закрытых запросов нет." if closed else "Открытых запросов нет."
        text += f"\n\n{empty}"
    await respond(target, text, b.as_markup())


def _my_list_args(data: str) -> tuple[bool, int]:
    parts = data.split(":")
    view = parts[1] if len(parts) > 1 else "o"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return view == "c", page


@router.callback_query(F.data == SEC_MY_REQUESTS)
async def open_my_requests(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    await state.clear()
    await show_my_requests(cb, repo, actor.student.id)


@router.callback_query(F.data == "rqmy")
async def my_requests_from_wizard(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    await state.set_state(None)
    await state.update_data(req_from="wizard")
    data = await state.get_data()
    await show_my_requests(cb, repo, actor.student.id, back=_list_back(data))


@router.callback_query(F.data.startswith("mrqv:"))
async def my_requests_view(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    await state.set_state(None)
    closed, page = _my_list_args(cb.data)
    await show_my_requests(cb, repo, actor.student.id, closed=closed, page=page, back=_list_back(await state.get_data()))


@router.callback_query(F.data.startswith("mrqp:"))
async def my_requests_page(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    closed, page = _my_list_args(cb.data)
    await show_my_requests(cb, repo, actor.student.id, closed=closed, page=page, back=_list_back(await state.get_data()))


@router.callback_query(F.data.startswith("mrqi:"))
async def my_request_card(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    await state.set_state(None)
    req = await repo.get_request(int(cb.data.split(":", 1)[1]))
    if req is None or req.student_id != actor.student.id:
        await answer_callback(cb, "Запрос не найден", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    if req.status == "open":
        b.row(btn("Отменить запрос", f"mrqc:{req.id}"))
    b.row(*nav_row("mrqv:o" if req.status == "open" else "mrqv:c"))
    await respond(cb, await _request_text(repo, config, req), b.as_markup())


@router.callback_query(F.data.startswith("mrqc:"))
async def cancel_my_request_ask(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    req = await repo.get_request(int(cb.data.split(":", 1)[1]))
    if req is None or req.student_id != actor.student.id:
        await answer_callback(cb, "Запрос не найден", show_alert=True)
        return
    if req.status != "open":
        await answer_callback(cb, "Запрос уже обработан.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.row(btn("Да, отменить", f"mrqx:{req.id}"))
    b.row(*nav_row(f"mrqi:{req.id}"))
    await respond(cb, "Отменить запрос? После этого по этому занятию можно отправить новый.", b.as_markup())


@router.callback_query(F.data.startswith("mrqx:"))
async def cancel_my_request(cb: CallbackQuery, repo: Repo, config: Config, bot: Bot, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    req = await repo.get_request(int(cb.data.split(":", 1)[1]))
    if req is None or req.student_id != actor.student.id:
        await answer_callback(cb, "Запрос не найден", show_alert=True)
        return
    cancelled = await repo.cancel_request(req.id, actor.student.id, actor.telegram_id)
    if cancelled is None:
        await answer_callback(cb, "Запрос уже обработан.", show_alert=True)
        return
    event = await repo.get_event(cancelled.event_id)
    await audit(
        repo,
        actor,
        "request.cancel",
        entity_type="request",
        entity_id=cancelled.id,
        занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}" if event else None,
    )
    if event is not None and event.created_by != actor.telegram_id:
        await safe_send_message(
            bot,
            event.created_by,
            f"↩️ {esc(actor.student.full_name)} отменил запрос\n{event_title_line(event)}",
        )
    b = InlineKeyboardBuilder()
    b.row(*nav_row("mrqv:c"))
    await respond(cb, await _request_text(repo, config, cancelled), b.as_markup(), notice="Отменено")


# ------------------------------------------------------------- marker side


def _request_actions_kb(request_id: int):
    b = InlineKeyboardBuilder()
    b.row(btn("✅ Отметить", f"rqa:{request_id}"), btn("❌ Отклонить", f"rqr:{request_id}"))
    b.row(btn("📨 Все запросы", SEC_REQUESTS))
    return b.as_markup()


def _requests_inbox_kb():
    b = InlineKeyboardBuilder()
    b.row(btn("📨 Все запросы", SEC_REQUESTS))
    return b.as_markup()


async def _request_text(repo: Repo, config: Config, req: Request) -> str:
    student = await repo.get_student(req.student_id)
    event = await repo.get_event(req.event_id)
    lines = [
        f"📨 <b>Запрос #{req.id}</b> · {STATUS_TITLES.get(req.status, req.status)}",
        f"От: {esc(student.full_name) if student else req.student_id}",
        f"Занятие: {event_title_line(event) if event else '—'}",
        f"Отправлен: {format_dt_iso(req.created_at, config.timezone)}",
        "",
        f"«{esc(req.text)}»",
    ]
    if req.status == "cancelled":
        lines.append("")
        lines.append(f"Студент отменил запрос · {format_dt_iso(req.resolved_at, config.timezone)}")
    elif req.status != "open":
        resolver = await repo.get_student_by_telegram(req.resolved_by) if req.resolved_by else None
        who = resolver.full_name if resolver else (req.resolved_by or "—")
        lines.append("")
        lines.append(f"Решение: {STATUS_TITLES.get(req.status)} · {esc(who)} · {format_dt_iso(req.resolved_at, config.timezone)}")
        if req.resolution_comment:
            lines.append(f"Комментарий: {esc(req.resolution_comment)}")
    return "\n".join(lines)


async def show_requests_list(target: CallbackQuery | Message, repo: Repo, *, resolved: bool = False, page: int = 0) -> None:
    if resolved:
        items = await repo.list_requests(status=None, closed_only=True, limit=200)
        title = "📁 <b>Обработанные запросы</b>"
        page_prefix = "rqhp"
    else:
        items = await repo.list_requests(status="open")
        title = "📨 <b>Запросы студентов</b>"
        page_prefix = "rqlp"
    chunk, page, pages = _paged(items, page, REQUEST_PAGE)
    b = InlineKeyboardBuilder()
    for req in chunk:
        student = await repo.get_student(req.student_id)
        event = await repo.get_event(req.event_id)
        name = truncate(student.full_name, 22) if student else "?"
        ev = f"{event.day.strftime('%d.%m')} {truncate(event.title, 14)}" if event else "?"
        icon = {"approved": "✅", "rejected": "❌", "cancelled": "↩️"}.get(req.status, "•")
        b.row(btn(f"{icon} {name} · {ev}", f"rq:{req.id}"))
    if pages > 1:
        b.row(*pager_row(page_prefix, page, pages))
    if resolved:
        b.row(btn("📨 Открытые", SEC_REQUESTS))
    else:
        b.row(btn("📁 Обработанные", "rqh"))
    b.row(*nav_row())
    if not items:
        text = title + "\n\nПока пусто."
    else:
        text = title + "\n\nНажмите на запрос, чтобы рассмотреть."
    await respond(target, text, b.as_markup())


@router.callback_query(F.data == SEC_REQUESTS)
async def open_requests(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_requests_list(cb, repo)


@router.callback_query(F.data == "rqh")
async def resolved_requests(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_requests_list(cb, repo, resolved=True)


@router.callback_query(F.data.startswith("rqlp:"))
async def requests_page(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await show_requests_list(cb, repo, page=int(cb.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("rqhp:"))
async def resolved_requests_page(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await show_requests_list(cb, repo, resolved=True, page=int(cb.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("rq:"))
async def request_card(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    req = await repo.get_request(int(cb.data.split(":", 1)[1]))
    if req is None:
        await answer_callback(cb, "Запрос не найден", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    if req.status == "open":
        b.row(btn("✅ Отметить", f"rqa:{req.id}"), btn("❌ Отклонить", f"rqr:{req.id}"))
    b.row(*nav_row(SEC_REQUESTS if req.status == "open" else "rqh"))
    await respond(cb, await _request_text(repo, config, req), b.as_markup())


@router.callback_query(F.data.startswith("rqa:"))
async def approve_request(cb: CallbackQuery, repo: Repo, config: Config, bot: Bot, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    req = await repo.get_request(int(cb.data.split(":", 1)[1]))
    if req is None or req.status != "open":
        await answer_callback(cb, "Запрос уже обработан.", show_alert=True)
        return
    student = await repo.get_student(req.student_id)
    event = await repo.get_event(req.event_id)
    if student is None or event is None or event.deleted_at:
        await answer_callback(cb, "Студент или занятие не найдены.", show_alert=True)
        return
    approval = await repo.approve_request_with_attendance(req.id, actor.telegram_id)
    if approval.code == "resolved":
        await answer_callback(cb, "Запрос уже обработан.", show_alert=True)
        return
    if approval.code == "missing":
        await answer_callback(cb, "Студент или занятие не найдены.", show_alert=True)
        return
    if approval.code == "not_counted" or approval.request is None:
        await answer_callback(cb, "Студент не учитывается на эту дату, отметка не войдёт в процент. Запрос не закрыт.", show_alert=True)
        return
    req = approval.request
    changed = approval.marked
    if changed:
        await audit(repo, actor, "attendance.mark", entity_type="event", entity_id=event.id, студент=student.full_name, занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}", по_запросу=req.id)
    await audit(repo, actor, "request.approve", entity_type="request", entity_id=req.id, студент=student.full_name, занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}")
    if student.telegram_id:
        await safe_send_message(bot, student.telegram_id, f"✅ Ваш запрос одобрен: вы отмечены на занятии\n{event_title_line(event)}")
    await respond(cb, await _request_text(repo, config, req), _back_kb(), notice="Отмечено")


@router.callback_query(F.data.startswith("rqr:"))
async def reject_request_start(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    req = await repo.get_request(int(cb.data.split(":", 1)[1]))
    if req is None or req.status != "open":
        await answer_callback(cb, "Запрос уже обработан.", show_alert=True)
        return
    await state.set_state(RejectSG.comment)
    await state.update_data(reject_id=req.id)
    b = InlineKeyboardBuilder()
    b.row(btn("Отклонить без комментария", "rqrn"))
    b.row(*nav_row(f"rq:{req.id}"))
    await respond(cb, "Напишите причину отказа одним сообщением (студент её увидит) или отклоните без комментария.", b.as_markup())


async def _reject(target: CallbackQuery | Message, state: FSMContext, repo: Repo, config: Config, bot: Bot, actor: Actor, comment: str | None) -> None:
    if not await require_level(target, actor):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    req = await repo.get_request(int(data.get("reject_id") or 0))
    if req is None or req.status != "open":
        await respond(target, "Запрос уже обработан.", _back_kb())
        return
    student = await repo.get_student(req.student_id)
    event = await repo.get_event(req.event_id)
    req = await repo.resolve_request(req.id, "rejected", actor.telegram_id, comment)
    if req is None:
        await respond(target, "Запрос уже обработан.", _back_kb())
        return
    await audit(repo, actor, "request.reject", entity_type="request", entity_id=req.id, студент=student.full_name if student else None, комментарий=comment)
    if student and student.telegram_id:
        text = "❌ Ваш запрос отклонён"
        if event:
            text += f"\n{event_title_line(event)}"
        if comment:
            text += f"\nКомментарий: {esc(comment)}"
        await safe_send_message(bot, student.telegram_id, text)
    await respond(target, await _request_text(repo, config, req), _back_kb(), notice="Отклонено")


def _back_kb():
    b = InlineKeyboardBuilder()
    b.row(*nav_row(SEC_REQUESTS))
    return b.as_markup()


@router.callback_query(RejectSG.comment, F.data == "rqrn")
async def reject_no_comment(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, bot: Bot, actor: Actor) -> None:
    await _reject(cb, state, repo, config, bot, actor, None)


@router.message(RejectSG.comment, F.text)
async def reject_with_comment(message: Message, state: FSMContext, repo: Repo, config: Config, bot: Bot, actor: Actor) -> None:
    comment = " ".join((message.text or "").split())[:300]
    await _reject(message, state, repo, config, bot, actor, comment or None)
