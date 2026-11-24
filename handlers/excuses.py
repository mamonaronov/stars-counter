"""Sick leaves and approved absences: missed mandatory events in the range count as attended.

An open sick leave or approved absence has no end date and covers every day from its start until it is closed.
"""

from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.models import Excuse
from database.queries import Repo
from handlers.common import EXCUSE_TITLES, require_level, respond, show_student_picker
from keyboards.main import btn, cancel_kb, confirm_kb, nav_row, pager_row
from services.audit import audit
from services.roles import Actor
from states import ExcuseSG
from utils.callbacks import SEC_EXCUSES
from utils.formatting import esc, truncate
from utils.telegram import answer_callback
from utils.time import format_dt_iso, local_today, parse_date_text

router = Router(name="excuses")

PICK_CTX = "exc"
KIND_ICON = {"sick": "🤒", "leave": "📝"}


def _range(excuse: Excuse) -> str:
    start = date.fromisoformat(excuse.date_from).strftime("%d.%m.%Y")
    if excuse.date_to is None:
        return f"с {start}, открыт"
    end = date.fromisoformat(excuse.date_to).strftime("%d.%m.%Y")
    return start if start == end else f"{start} — {end}"


EXCUSE_PAGE = 8

_STATUSES = ("open", "closed")
_KINDS = ("all", "sick", "leave")
_STATUS_LABEL = {"open": "Открытые", "closed": "Закрытые"}
_KIND_LABEL = {"all": "Все", "sick": "🤒 Больничные", "leave": "📝 Отпросы"}
_STATUS_HINT = {
    "open": "Действуют с даты начала, пока их не закроют.",
    "closed": "Покрывают только дни внутри указанных дат.",
}
_KIND_NAME = {"all": "записей", "sick": "больничных", "leave": "отпросов"}
_STATUS_NAME = {"open": "Открытых", "closed": "Закрытых"}


def _tab(text: str, on: bool) -> str:
    return f"[{text}]" if on else text


def _is_status(excuse: Excuse, status: str) -> bool:
    return excuse.is_open if status == "open" else not excuse.is_open


def _is_kind(excuse: Excuse, kind: str) -> bool:
    return kind == "all" or excuse.kind == kind


def _filters(data: str) -> tuple[str, str, int]:
    """Status, kind and page from a list callback. Old buttons without a kind still open."""
    status, kind, page = "open", "all", 0
    tokens = data.split(":")[1:]
    if tokens and tokens[0] in _STATUSES:
        status = tokens.pop(0)
    elif tokens and tokens[0] == "leave":
        kind = "leave"
        tokens.pop(0)
    if tokens and tokens[0] in _KINDS:
        kind = tokens.pop(0)
    if tokens and tokens[0].isdigit():
        page = int(tokens[0])
    return status, kind, page


async def show_excuses(
    target: CallbackQuery | Message,
    repo: Repo,
    page: int = 0,
    *,
    status: str = "open",
    kind: str = "all",
) -> None:
    if status not in _STATUSES:
        status = "open"
    if kind not in _KINDS:
        kind = "all"
    items = await repo.list_excuses(active_only=True)
    in_status = [excuse for excuse in items if _is_status(excuse, status)]
    current = [excuse for excuse in in_status if _is_kind(excuse, kind)]
    pages = max(1, (len(current) + EXCUSE_PAGE - 1) // EXCUSE_PAGE)
    page = max(0, min(page, pages - 1))
    open_count = sum(1 for excuse in items if excuse.is_open and _is_kind(excuse, kind))
    closed_count = sum(1 for excuse in items if not excuse.is_open and _is_kind(excuse, kind))
    b = InlineKeyboardBuilder()
    b.row(
        btn(_tab(f"Открытые ({open_count})", status == "open"), f"excv:open:{kind}"),
        btn(_tab(f"Закрытые ({closed_count})", status == "closed"), f"excv:closed:{kind}"),
    )
    b.row(
        btn(_tab(f"Все ({len(in_status)})", kind == "all"), f"excv:{status}:all"),
        btn(_tab(f"Больничные ({sum(1 for excuse in in_status if excuse.kind == 'sick')})", kind == "sick"), f"excv:{status}:sick"),
        btn(_tab(f"Отпросы ({sum(1 for excuse in in_status if excuse.kind == 'leave')})", kind == "leave"), f"excv:{status}:leave"),
    )
    for excuse in current[page * EXCUSE_PAGE : (page + 1) * EXCUSE_PAGE]:
        student = await repo.get_student(excuse.student_id)
        name = truncate(student.full_name, 22) if student else "?"
        b.row(btn(f"{KIND_ICON.get(excuse.kind, '•')} {name} · {_range(excuse)}", f"exc:{excuse.id}:{status}:{kind}"))
    if pages > 1:
        b.row(*pager_row(f"excp:{status}:{kind}", page, pages))
    b.row(btn("➕ Добавить", "exca"))
    b.row(*nav_row())
    title = _STATUS_LABEL[status]
    if kind != "all":
        title += f" · {_KIND_LABEL[kind]}"
    text = f"🩺 <b>Больничные и отпросы</b>\n\n<b>{title}</b>\n{_STATUS_HINT[status]}"
    if not current:
        text += f"\n\n{_STATUS_NAME[status]} {_KIND_NAME[kind]} нет."
    await respond(target, text, b.as_markup())


@router.callback_query(F.data == SEC_EXCUSES)
async def open_excuses(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_excuses(cb, repo, 0)


@router.callback_query(F.data.startswith("excv:"))
async def excuses_view(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    status, kind, page = _filters(cb.data)
    await show_excuses(cb, repo, page, status=status, kind=kind)


@router.callback_query(F.data.startswith("excp:"))
async def excuses_page(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    status, kind, page = _filters(cb.data)
    await show_excuses(cb, repo, page, status=status, kind=kind)


@router.callback_query(F.data == "exca")
async def add_excuse_start(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await show_student_picker(cb, state, repo, ctx=PICK_CTX, title="🩺 <b>Новый больничный / отпрос</b>\nВыберите студента:", back=SEC_EXCUSES)


@router.callback_query(F.data.startswith(f"pk:{PICK_CTX}:"))
async def add_excuse_student(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    student = await repo.get_student(int(cb.data.rsplit(":", 1)[1]))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    await state.clear()
    await state.set_state(ExcuseSG.kind)
    await state.update_data(exc_student_id=student.id, exc_student_name=student.full_name)
    b = InlineKeyboardBuilder()
    b.row(btn("🤒 Больничный", "exk:sick"), btn("📝 Отпрос", "exk:leave"))
    b.row(*nav_row(SEC_EXCUSES))
    await respond(cb, f"👤 <b>{esc(student.full_name)}</b>\n\nЧто оформляем?", b.as_markup())


@router.callback_query(ExcuseSG.kind, F.data.startswith("exk:"))
async def add_excuse_kind(cb: CallbackQuery, state: FSMContext, config: Config) -> None:
    kind = cb.data.split(":", 1)[1]
    await state.update_data(exc_kind=kind)
    await state.set_state(ExcuseSG.date_from)
    today = local_today(config.timezone)
    b = InlineKeyboardBuilder()
    b.row(btn("Сегодня", f"exdf:{today.isoformat()}"))
    b.row(*nav_row(SEC_EXCUSES))
    await respond(cb, f"{EXCUSE_TITLES[kind].capitalize()}.\n\nС какой даты? Напишите, например <code>12.09</code> или <code>12.09.2026</code>.", b.as_markup())


async def _ask_date_to(target: CallbackQuery | Message, state: FSMContext, day: date) -> None:
    data = await state.get_data()
    await state.update_data(exc_from=day.isoformat())
    await state.set_state(ExcuseSG.date_to)
    b = InlineKeyboardBuilder()
    b.row(btn("Только этот день", f"exdt:{day.isoformat()}"))
    b.row(btn("Открытый", "exopen"))
    b.row(*nav_row(SEC_EXCUSES))
    text = (
        f"С {day.strftime('%d.%m.%Y')}.\n\nПо какую дату (включительно)? "
        "«Открытый» — пока без даты окончания, её можно указать позже."
    )
    await respond(target, text, b.as_markup())


@router.callback_query(ExcuseSG.date_from, F.data.startswith("exdf:"))
async def add_excuse_from_btn(cb: CallbackQuery, state: FSMContext) -> None:
    await _ask_date_to(cb, state, date.fromisoformat(cb.data.split(":", 1)[1]))


@router.message(ExcuseSG.date_from, F.text)
async def add_excuse_from_text(message: Message, state: FSMContext, config: Config) -> None:
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        await message.answer("Не понял дату. Пример: <code>12.09.2026</code>.", reply_markup=cancel_kb(SEC_EXCUSES))
        return
    await _ask_date_to(message, state, day)


async def _ask_comment(target: CallbackQuery | Message, state: FSMContext, day_to: date | None) -> None:
    data = await state.get_data()
    day_from = date.fromisoformat(data["exc_from"])
    if day_to is not None and day_to < day_from:
        await respond(target, "Дата окончания раньше даты начала. Введите дату окончания ещё раз.", cancel_kb(SEC_EXCUSES))
        return
    # Empty string survives FSM storage; NULL in the database means the record is still open.
    await state.update_data(exc_to="" if day_to is None else day_to.isoformat())
    await state.set_state(ExcuseSG.comment)
    b = InlineKeyboardBuilder()
    b.row(btn("Без комментария", "exnc"))
    b.row(*nav_row(SEC_EXCUSES))
    if day_to is None:
        period = f"С {day_from.strftime('%d.%m.%Y')}, открытый"
    else:
        period = f"{day_from.strftime('%d.%m.%Y')} — {day_to.strftime('%d.%m.%Y')}"
    await respond(target, f"{period}.\n\nКомментарий (номер справки, причина)?", b.as_markup())


@router.callback_query(ExcuseSG.date_to, F.data == "exopen")
async def add_excuse_open(cb: CallbackQuery, state: FSMContext) -> None:
    await _ask_comment(cb, state, None)


@router.callback_query(ExcuseSG.date_to, F.data.startswith("exdt:"))
async def add_excuse_to_btn(cb: CallbackQuery, state: FSMContext) -> None:
    await _ask_comment(cb, state, date.fromisoformat(cb.data.split(":", 1)[1]))


@router.message(ExcuseSG.date_to, F.text)
async def add_excuse_to_text(message: Message, state: FSMContext, config: Config) -> None:
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        await message.answer("Не понял дату. Пример: <code>20.09.2026</code>.", reply_markup=cancel_kb(SEC_EXCUSES))
        return
    await _ask_comment(message, state, day)


async def _create(target: CallbackQuery | Message, state: FSMContext, repo: Repo, config: Config, actor: Actor, comment: str | None) -> None:
    if not await require_level(target, actor):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    date_to = data.get("exc_to") or None
    excuse = await repo.create_excuse(int(data["exc_student_id"]), data["exc_kind"], data["exc_from"], date_to, comment, actor.telegram_id)
    await audit(
        repo,
        actor,
        "excuse.add",
        entity_type="excuse",
        entity_id=excuse.id,
        студент=data.get("exc_student_name"),
        вид=EXCUSE_TITLES[excuse.kind],
        период=_range(excuse),
        комментарий=comment,
    )
    await show_excuse_card(target, repo, config, excuse.id, notice="Сохранено")


@router.callback_query(ExcuseSG.comment, F.data == "exnc")
async def add_excuse_no_comment(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    await _create(cb, state, repo, config, actor, None)


@router.message(ExcuseSG.comment, F.text)
async def add_excuse_comment(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    comment = " ".join((message.text or "").split())[:200]
    await _create(message, state, repo, config, actor, comment or None)


async def show_excuse_card(
    target: CallbackQuery | Message,
    repo: Repo,
    config: Config,
    excuse_id: int,
    *,
    status: str | None = None,
    kind: str | None = None,
    notice: str | None = None,
) -> None:
    excuse = await repo.get_excuse(excuse_id)
    if excuse is None:
        if isinstance(target, CallbackQuery):
            await answer_callback(target, "Запись не найдена", show_alert=True)
        return
    student = await repo.get_student(excuse.student_id)
    author = await repo.get_student_by_telegram(excuse.created_by)
    lines = [
        f"{KIND_ICON.get(excuse.kind, '•')} <b>{EXCUSE_TITLES.get(excuse.kind, excuse.kind).capitalize()}</b>",
        f"Студент: {esc(student.full_name) if student else excuse.student_id}",
        f"Период: {_range(excuse)}",
        f"Комментарий: {esc(excuse.comment) if excuse.comment else '—'}",
        f"Оформил: {esc(author.full_name) if author else excuse.created_by} · {format_dt_iso(excuse.created_at, config.timezone)}",
    ]
    if excuse.cancelled_at:
        lines.append(f"<b>Отменён</b> {format_dt_iso(excuse.cancelled_at, config.timezone)}")
    b = InlineKeyboardBuilder()
    if excuse.is_active and excuse.is_open:
        b.row(btn("📅 Закрыть", f"exclose:{excuse.id}"))
    if excuse.is_active:
        b.row(btn("❌ Отменить", f"excc:{excuse.id}"))
    back_status = status if status in _STATUSES else ("open" if excuse.is_open else "closed")
    back_kind = kind if kind in _KINDS else "all"
    b.row(*nav_row(f"excv:{back_status}:{back_kind}"))
    await respond(target, "\n".join(lines), b.as_markup(), notice=notice)


@router.callback_query(F.data.startswith("exc:"))
async def excuse_card(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    parts = cb.data.split(":")
    status = parts[2] if len(parts) > 2 and parts[2] in _STATUSES else None
    kind = parts[3] if len(parts) > 3 and parts[3] in _KINDS else None
    await show_excuse_card(cb, repo, config, int(parts[1]), status=status, kind=kind)


@router.callback_query(F.data.startswith("excc:"))
async def cancel_excuse_ask(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    excuse = await repo.get_excuse(int(cb.data.split(":", 1)[1]))
    if excuse is None or not excuse.is_active:
        await answer_callback(cb, "Запись не найдена или уже отменена", show_alert=True)
        return
    await respond(cb, f"Отменить {EXCUSE_TITLES.get(excuse.kind, '')} за {_range(excuse)}? Пропуски снова будут считаться пропусками.", confirm_kb(f"exccc:{excuse.id}", f"exc:{excuse.id}", yes_text="❌ Отменить запись"))


@router.callback_query(F.data.startswith("exccc:"))
async def cancel_excuse(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    excuse = await repo.get_excuse(int(cb.data.split(":", 1)[1]))
    if excuse is None or not excuse.is_active:
        await answer_callback(cb, "Запись не найдена или уже отменена", show_alert=True)
        return
    student = await repo.get_student(excuse.student_id)
    await repo.cancel_excuse(excuse.id, actor.telegram_id)
    await audit(repo, actor, "excuse.cancel", entity_type="excuse", entity_id=excuse.id, студент=student.full_name if student else None, вид=EXCUSE_TITLES.get(excuse.kind), период=_range(excuse))
    await show_excuse_card(cb, repo, config, excuse.id, notice="Отменено")


def _close_keyboard(excuse_id: int, today: date):
    b = InlineKeyboardBuilder()
    b.row(btn("Сегодня", f"excdt:{today.isoformat()}"))
    b.row(*nav_row(f"exc:{excuse_id}"))
    return b.as_markup()


async def _close_excuse(target: CallbackQuery | Message, state: FSMContext, repo: Repo, config: Config, actor: Actor | None, day: date) -> None:
    if not await require_level(target, actor):
        await state.clear()
        return
    assert actor is not None
    data = await state.get_data()
    excuse_id = int(data["exc_close_id"])
    start = date.fromisoformat(data["exc_from"])
    if day < start:
        await respond(
            target,
            "Дата окончания раньше даты начала. Введите дату ещё раз.",
            _close_keyboard(excuse_id, local_today(config.timezone)),
        )
        return
    closed = await repo.close_excuse(excuse_id, day.isoformat())
    await state.clear()
    if closed is None:
        if isinstance(target, CallbackQuery):
            await answer_callback(target, "Запись уже закрыта или отменена", show_alert=True)
        await show_excuse_card(target, repo, config, excuse_id)
        return
    student = await repo.get_student(closed.student_id)
    await audit(
        repo,
        actor,
        "excuse.close",
        entity_type="excuse",
        entity_id=closed.id,
        студент=student.full_name if student else None,
        период=_range(closed),
    )
    title = EXCUSE_TITLES.get(closed.kind, "запись")
    await show_excuse_card(target, repo, config, closed.id, notice=f"{title.capitalize()} закрыт")


@router.callback_query(F.data.startswith("exclose:"))
async def close_excuse_start(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    excuse = await repo.get_excuse(int(cb.data.split(":", 1)[1]))
    if excuse is None or not excuse.is_active or not excuse.is_open:
        await answer_callback(cb, "Запись уже закрыта или отменена", show_alert=True)
        return
    await state.clear()
    await state.set_state(ExcuseSG.close_on)
    await state.update_data(exc_close_id=excuse.id, exc_from=excuse.date_from)
    start = date.fromisoformat(excuse.date_from).strftime("%d.%m.%Y")
    title = EXCUSE_TITLES.get(excuse.kind, "запись")
    await respond(
        cb,
        f"Открытый {title} с {start}.\n\nПо какую дату закрыть (включительно)?",
        _close_keyboard(excuse.id, local_today(config.timezone)),
    )


@router.callback_query(ExcuseSG.close_on, F.data.startswith("excdt:"))
async def close_excuse_btn(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    await _close_excuse(cb, state, repo, config, actor, date.fromisoformat(cb.data.split(":", 1)[1]))


@router.message(ExcuseSG.close_on, F.text)
async def close_excuse_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        data = await state.get_data()
        excuse_id = int(data.get("exc_close_id") or 0)
        back = f"exc:{excuse_id}" if excuse_id else SEC_EXCUSES
        await message.answer("Не понял дату. Пример: <code>20.09.2026</code>.", reply_markup=cancel_kb(back))
        return
    await _close_excuse(message, state, repo, config, actor, day)
