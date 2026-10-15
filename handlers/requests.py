"""Student requests: "you forgot to mark me" — created by students, handled by markers."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.models import Request
from database.queries import Repo
from handlers.common import event_title_line, load_periods, require_level, require_verified, respond, show_main
from keyboards.main import btn, cancel_kb, nav_row, pager_row
from services.audit import audit
from services.periods import find_period, period_button_label
from services.roles import Actor
from states import RejectSG, RequestSG
from utils.callbacks import SEC_NEW_REQUEST, SEC_REQUESTS
from utils.formatting import esc, truncate
from utils.telegram import answer_callback, safe_send_message
from utils.time import format_dt_iso

router = Router(name="requests")

STATUS_TITLES = {"open": "ожидает", "approved": "одобрен", "rejected": "отклонён"}
PERIOD_PAGE = 6
EVENT_PAGE = 8


# ------------------------------------------------------------ student side


@router.callback_query(F.data == SEC_NEW_REQUEST)
async def new_request_start(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    await state.clear()
    periods = await load_periods(repo, config)
    await _show_request_periods(cb, periods, 0)


def _paged(items: list, page: int, size: int) -> tuple[list, int, int]:
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    return items[page * size : (page + 1) * size], page, pages


async def _show_request_periods(cb: CallbackQuery, periods, page: int) -> None:
    ordered = list(reversed(periods))
    chunk, page, pages = _paged(ordered, page, PERIOD_PAGE)
    b = InlineKeyboardBuilder()
    for period in chunk:
        b.row(btn(period_button_label(period), f"rqp:{period.key}"))
    if pages > 1:
        b.row(*pager_row("rqpg", page, pages))
    b.row(*nav_row())
    await respond(cb, "✉️ <b>Запрос отмечающему</b>\n\nЗа какой месяц занятие, на котором вас забыли отметить?", b.as_markup())


@router.callback_query(F.data.startswith("rqpg:"))
async def request_periods_page(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    periods = await load_periods(repo, config)
    await _show_request_periods(cb, periods, int(cb.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("rqp:"))
async def new_request_period(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    key = cb.data.split(":", 1)[1]
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    await _show_request_events(cb, repo, actor, period, 0)


async def _show_request_events(cb: CallbackQuery, repo: Repo, actor: Actor, period, page: int) -> None:
    assert actor.student is not None
    present = await repo.attendance_of_student(actor.student.id)
    candidates = [e for e in reversed(period.events) if e.id not in present and actor.student.counts_on(e.day)]
    if not candidates:
        await answer_callback(cb, "В этом месяце нет занятий, где вы не отмечены.", show_alert=True)
        return
    chunk, page, pages = _paged(candidates, page, EVENT_PAGE)
    b = InlineKeyboardBuilder()
    for event in chunk:
        time = f" {event.start_time}" if event.start_time else ""
        kind = "" if event.is_mandatory else " (св.)"
        b.row(btn(f"{event.day.strftime('%d.%m')}{time} · {truncate(event.title, 26)}{kind}", f"rqe:{event.id}"))
    if pages > 1:
        b.row(*pager_row(f"rqel:{period.key}", page, pages))
    b.row(*nav_row(SEC_NEW_REQUEST))
    await respond(cb, f"📅 <b>{esc(period.title)}</b>\n\nЗанятия, где вы не отмечены. Выберите нужное:", b.as_markup())


@router.callback_query(F.data.startswith("rqel:"))
async def request_events_page(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None
    _, key, raw_page = cb.data.split(":", 2)
    periods = await load_periods(repo, config)
    period = find_period(periods, key)
    if period is None:
        await answer_callback(cb, "Период не найден", show_alert=True)
        return
    await _show_request_events(cb, repo, actor, period, int(raw_page))


@router.callback_query(F.data.startswith("rqe:"))
async def new_request_event(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_verified(cb, actor):
        return
    assert actor is not None and actor.student is not None
    event = await repo.get_event(int(cb.data.split(":", 1)[1]))
    if event is None or event.deleted_at:
        await answer_callback(cb, "Занятие не найдено", show_alert=True)
        return
    if await repo.is_present(event.id, actor.student.id):
        await answer_callback(cb, "Вы уже отмечены на этом занятии.", show_alert=True)
        return
    if await repo.has_open_request(actor.student.id, event.id):
        await answer_callback(cb, "По этому занятию уже есть необработанный запрос.", show_alert=True)
        return
    await state.set_state(RequestSG.text)
    await state.update_data(req_event_id=event.id)
    await respond(
        cb,
        f"{event_title_line(event)}\n\nНапишите сообщение отмечающему: почему вас нужно отметить (например, «был, сидел на третьем ряду»).",
        cancel_kb(SEC_NEW_REQUEST),
    )


@router.message(RequestSG.text, F.text)
async def new_request_text(message: Message, state: FSMContext, repo: Repo, config: Config, bot: Bot, actor: Actor) -> None:
    text = " ".join((message.text or "").split())[:500]
    if not text:
        await message.answer("Сообщение не может быть пустым.")
        return
    data = await state.get_data()
    await state.clear()
    event = await repo.get_event(int(data.get("req_event_id") or 0))
    if event is None or event.deleted_at or actor.student is None:
        await message.answer("Занятие не найдено.")
        return
    if not actor.student.counts_on(event.day):
        await message.answer("Это занятие вам не учитывается, запрос не отправлен.")
        await show_main(message, actor, config, repo)
        return
    if await repo.is_present(event.id, actor.student.id):
        await message.answer("Вас уже отметили на этом занятии.")
        await show_main(message, actor, config, repo)
        return
    if await repo.has_open_request(actor.student.id, event.id):
        await message.answer("По этому занятию уже есть необработанный запрос.")
        await show_main(message, actor, config, repo)
        return
    req = await repo.create_request(actor.student.id, event.id, text)
    await audit(repo, actor, "request.create", entity_type="request", entity_id=req.id, занятие=f"{event.day.strftime('%d.%m.%Y')} {event.title}", текст=text)
    notify = (
        f"📨 <b>Новый запрос</b> от {esc(actor.student.full_name)}\n"
        f"{event_title_line(event)}\n\n«{esc(text)}»"
    )
    await safe_send_message(bot, event.created_by, notify, reply_markup=_request_actions_kb(req.id))
    await message.answer("✅ Запрос отправлен. Вам придёт сообщение, когда отмечающий его рассмотрит.")
    await show_main(message, actor, config, repo)


# ------------------------------------------------------------- marker side


def _request_actions_kb(request_id: int):
    b = InlineKeyboardBuilder()
    b.row(btn("✅ Отметить", f"rqa:{request_id}"), btn("❌ Отклонить", f"rqr:{request_id}"))
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
    if req.status != "open":
        resolver = await repo.get_student_by_telegram(req.resolved_by) if req.resolved_by else None
        who = resolver.full_name if resolver else (req.resolved_by or "—")
        lines.append("")
        lines.append(f"Решение: {STATUS_TITLES.get(req.status)} · {esc(who)} · {format_dt_iso(req.resolved_at, config.timezone)}")
        if req.resolution_comment:
            lines.append(f"Комментарий: {esc(req.resolution_comment)}")
    return "\n".join(lines)


REQUEST_PAGE = 8


async def show_requests_list(target: CallbackQuery | Message, repo: Repo, *, resolved: bool = False, page: int = 0) -> None:
    if resolved:
        items = [r for r in await repo.list_requests(status=None, limit=200) if r.status != "open"]
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
        icon = {"approved": "✅", "rejected": "❌"}.get(req.status, "•")
        b.row(btn(f"{icon} {name} · {ev}", f"rq:{req.id}"))
    if pages > 1:
        b.row(*pager_row(page_prefix, page, pages))
    if resolved:
        b.row(btn("📨 Открытые", SEC_REQUESTS))
    else:
        b.row(btn("📁 Обработанные", "rqh"))
    b.row(*nav_row())
    text = title + ("\n\nПока пусто." if not items else "\n\nНажмите на запрос, чтобы рассмотреть.")
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
    b.row(*nav_row(SEC_REQUESTS))
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
    if not student.counts_on(event.day):
        await answer_callback(cb, "Студент не учитывается на эту дату, отметка не войдёт в процент. Запрос не закрыт.", show_alert=True)
        return
    req = await repo.resolve_request(req.id, "approved", actor.telegram_id, None)
    if req is None:
        await answer_callback(cb, "Запрос уже обработан.", show_alert=True)
        return
    changed = await repo.set_presence(event.id, student.id, True, actor.telegram_id)
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
