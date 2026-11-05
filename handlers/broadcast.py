"""Markers send one message to chosen groups of people who have opened the bot."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.queries import Repo
from handlers.common import require_level, respond
from keyboards.main import back_kb, btn, fit_page, nav_row
from services.audit import audit
from services.broadcast import (
    FIXED_LABELS,
    FIXED_ORDER,
    PROFILE_KINDS,
    PROFILE_TITLES,
    AudienceCatalog,
    BroadcastPerson,
    audience_catalog,
    broadcast_people,
    broadcast_recipients,
    known_code,
    option_label,
    selected_labels,
    toggle_group,
)
from services.roles import LEVEL_MARKER, Actor
from states import BroadcastSG
from utils.callbacks import NAV_MAIN, NOOP, SEC_BROADCAST
from utils.formatting import esc, truncate
from utils.telegram import answer_callback, safe_edit

router = Router(name="broadcast")
logger = logging.getLogger(__name__)

TEXT_LIMIT = 3500
PREVIEW_LIMIT = 700
_FLOOD_WAIT_LIMIT = 30


def _catalog_from(data: dict) -> AudienceCatalog:
    years: list[int] = []
    for item in data.get("bc_years") or []:
        try:
            years.append(int(item))
        except (TypeError, ValueError):
            continue
    return AudienceCatalog(
        directions=tuple(str(item) for item in data.get("bc_directions") or []),
        groups=tuple(str(item) for item in data.get("bc_groups") or []),
        tracks=tuple(str(item) for item in data.get("bc_tracks") or []),
        years=tuple(years),
    )


def _selected_from(data: dict) -> set[str]:
    return {item for item in data.get("bc_selected") or [] if isinstance(item, str)}


async def _load(state: FSMContext, repo: Repo, config: Config) -> tuple[AudienceCatalog, set[str], list[BroadcastPerson]]:
    data = await state.get_data()
    students = await repo.list_students(active_only=False)
    users = await repo.list_users()
    people = broadcast_people(users, students, config.superadmin_id)
    return _catalog_from(data), _selected_from(data), people


def _audience_text(selected: set[str], catalog: AudienceCatalog, people: list[BroadcastPerson], actor_id: int) -> str:
    labels = selected_labels(selected, catalog)
    count = len(broadcast_recipients(people, selected, catalog, exclude=actor_id))
    chosen = ", ".join(labels) if labels else "—"
    lines = [
        "📢 <b>Сообщение пользователям</b>",
        "",
        "Отметьте, кому отправить. Можно несколько групп: сообщение получат все, кто входит хотя бы в одну. "
        "Один человек из нескольких групп считается один раз. «Все» снимает остальные отметки. Вам копия не придёт.",
        "",
        "<b>Кто в группах</b>",
        "• <b>Все</b> — каждый, кто открывал бота.",
        "• <b>Гости</b> — открывали бота, но не привязаны к карточке студента.",
        "• <b>Студенты</b> — верифицированы и без роли.",
        "• <b>Отмечающие</b>, <b>назначающие отмечающие</b>, <b>главные отмечающие</b>, <b>админы</b>, <b>главные админы</b>, <b>суперадмин</b> — только эта роль, без тех, кто выше.",
    ]
    profile_names = [PROFILE_TITLES[kind] for kind in PROFILE_KINDS if catalog.values(kind)]
    if profile_names:
        titled = ", ".join(f"<b>{esc(name)}</b>" for name in profile_names)
        lines.append(f"• {titled} — верифицированные студенты с выбранным значением, в том числе если у них есть роль.")
    lines += [
        "",
        f"Получат: <b>{count}</b>",
        f"Выбрано: {esc(chosen)}",
        "",
        "Число на кнопке — сколько человек в группе, кроме вас.",
    ]
    return "\n".join(lines)


def _count(people: list[BroadcastPerson], code: str, catalog: AudienceCatalog, actor_id: int) -> int:
    return len(broadcast_recipients(people, {code}, catalog, exclude=actor_id))


def _marked(label: str, on: bool, count: int) -> str:
    prefix = "✅ " if on else ""
    return truncate(f"{prefix}{label} · {count}", 64)


def _audience_kb(selected: set[str], catalog: AudienceCatalog, people: list[BroadcastPerson], actor_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    rows = (
        ("all",),
        ("g", "s"),
        ("m", "am"),
        ("cm", "a"),
        ("ca", "sa"),
    )
    for row in rows:
        b.row(
            *[
                btn(
                    _marked(FIXED_LABELS[code], code in selected, _count(people, code, catalog, actor_id)),
                    f"bc:x:{code}",
                )
                for code in row
            ]
        )
    profile_row = []
    for kind in PROFILE_KINDS:
        if not catalog.values(kind):
            continue
        picked = sum(1 for code in selected if option_label(code, catalog) and code.startswith(kind) and code[1:].isdigit())
        label = PROFILE_TITLES[kind]
        if picked:
            label = f"{label} · {picked}"
        profile_row.append(btn(label, f"bc:o:{kind}:0"))
        if len(profile_row) == 2:
            b.row(*profile_row)
            profile_row = []
    if profile_row:
        b.row(*profile_row)
    b.row(btn("✉️ Дальше", "bc:w"))
    b.row(*nav_row())
    return b.as_markup()


def _profile_kb(kind: str, catalog: AudienceCatalog, selected: set[str], people: list[BroadcastPerson], actor_id: int, page: int) -> InlineKeyboardMarkup:
    values = catalog.values(kind)
    b = InlineKeyboardBuilder()
    page, pages, size = fit_page(len(values), page, reserved=3)
    for offset, value in enumerate(values[page * size : (page + 1) * size]):
        index = page * size + offset
        code = f"{kind}{index}"
        b.row(btn(_marked(str(value), code in selected, _count(people, code, catalog, actor_id)), f"bc:x:{code}"))
    if pages > 1:
        b.row(
            btn("«", f"bc:o:{kind}:{page - 1}") if page > 0 else btn(" ", NOOP),
            btn(f"{page + 1}/{pages}", NOOP),
            btn("»", f"bc:o:{kind}:{page + 1}") if page < pages - 1 else btn(" ", NOOP),
        )
    b.row(btn("✉️ Дальше", "bc:w"))
    b.row(*nav_row("bc:home"))
    return b.as_markup()


_PROFILE_HELP = {
    "d": "Верифицированные студенты этого направления. Роль не важна: отмечающий с этим направлением тоже получит сообщение.",
    "j": "Верифицированные студенты этой группы иудаики. Роль не важна.",
    "t": "Верифицированные студенты с выбранным значением молитвы или тфилина. Роль не важна.",
    "y": "Верифицированные студенты этого года поступления. Роль не важна.",
}


def _profile_text(kind: str) -> str:
    return "\n".join(
        [
            f"📢 <b>{esc(PROFILE_TITLES[kind])}</b>",
            "",
            _PROFILE_HELP[kind],
            "",
            "Отметьте нужные значения. Можно несколько: человек попадёт в рассылку, если подходит хотя бы одно.",
        ]
    )


def _prompt_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("⬅️ Кому", "bc:home"))
    b.row(btn("✖️ Отмена", "n:c"))
    return b.as_markup()


def _confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(btn("✅ Отправить", "bc:ok"))
    b.row(btn("✏️ Изменить текст", "bc:ed"), btn("⬅️ Кому", "bc:home"))
    b.row(btn("🏠 Меню", NAV_MAIN))
    return b.as_markup()


def _confirm_text(body: str, selected: set[str], catalog: AudienceCatalog, people: list[BroadcastPerson], actor_id: int) -> str:
    labels = selected_labels(selected, catalog)
    count = len(broadcast_recipients(people, selected, catalog, exclude=actor_id))
    preview = body if len(body) <= PREVIEW_LIMIT else body[: PREVIEW_LIMIT - 1] + "…"
    return "\n".join(
        [
            f"📢 <b>Отправить {count} людям?</b>",
            "",
            f"Кому: {esc(', '.join(labels) if labels else '—')}",
            "",
            esc(preview),
        ]
    )


def outgoing_text(actor: Actor, body: str) -> str:
    head = f"📢 <b>Сообщение</b>\nОт: {esc(actor.display_name)} ({esc(actor.role_title)})\n\n"
    escaped = esc(body)
    room = 4096 - len(head)
    if len(escaped) > room:
        escaped = escaped[: max(room - 1, 0)] + "…"
    return head + escaped


async def _show_audience(target: CallbackQuery | Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    await state.set_state(None)
    catalog, selected, people = await _load(state, repo, config)
    text = _audience_text(selected, catalog, people, actor.telegram_id)
    markup = _audience_kb(selected, catalog, people, actor.telegram_id)
    await respond(target, text, markup)


async def _remember_catalog(state: FSMContext, repo: Repo) -> None:
    students = await repo.list_students(active_only=False)
    catalog = audience_catalog(students)
    await state.update_data(
        bc_selected=[],
        bc_directions=list(catalog.directions),
        bc_groups=list(catalog.groups),
        bc_tracks=list(catalog.tracks),
        bc_years=list(catalog.years),
        bc_text=None,
    )


@router.callback_query(F.data == SEC_BROADCAST)
async def open_broadcast(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    await state.clear()
    await _remember_catalog(state, repo)
    await _show_audience(cb, state, repo, config, actor)


@router.callback_query(F.data.startswith("bc:x:"))
async def toggle_broadcast_group(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None and cb.data is not None
    catalog, selected, people = await _load(state, repo, config)
    code = cb.data.removeprefix("bc:x:")
    if not known_code(code, catalog):
        await answer_callback(cb, "Такой группы нет", show_alert=True)
        return
    selected = toggle_group(selected, code)
    await state.update_data(bc_selected=sorted(selected))
    if code[0] in PROFILE_KINDS and code[1:].isdigit():
        await respond(cb, _profile_text(code[0]), _profile_kb(code[0], catalog, selected, people, actor.telegram_id, 0))
        return
    await _show_audience(cb, state, repo, config, actor)


@router.callback_query(F.data.startswith("bc:o:"))
async def open_profile_groups(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None and cb.data is not None
    rest = cb.data.removeprefix("bc:o:")
    kind, _, page_raw = rest.partition(":")
    if kind not in PROFILE_KINDS:
        await answer_callback(cb, "Такой группы нет", show_alert=True)
        return
    page = int(page_raw) if page_raw.isdigit() else 0
    catalog, selected, people = await _load(state, repo, config)
    if not catalog.values(kind):
        await answer_callback(cb, "Таких групп пока нет", show_alert=True)
        return
    await respond(cb, _profile_text(kind), _profile_kb(kind, catalog, selected, people, actor.telegram_id, page))


@router.callback_query(F.data == "bc:home")
async def back_to_audience(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    await _show_audience(cb, state, repo, config, actor)


@router.callback_query(F.data == "bc:w")
async def ask_broadcast_text(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    catalog, selected, people = await _load(state, repo, config)
    count = len(broadcast_recipients(people, selected, catalog, exclude=actor.telegram_id))
    if count == 0:
        await answer_callback(cb, "Сначала выберите, кому отправить. Кроме вас в выбранных группах никого нет.", show_alert=True)
        return
    labels = ", ".join(selected_labels(selected, catalog))
    await state.set_state(BroadcastSG.text)
    await respond(
        cb,
        "\n".join(
            [
                "📢 <b>Текст сообщения</b>",
                "",
                f"Его получат <b>{count}</b>: {esc(labels)}.",
                f"Напишите текст одним сообщением. Лимит — {TEXT_LIMIT} символов.",
            ]
        ),
        _prompt_kb(),
    )


@router.message(BroadcastSG.text, F.text)
async def save_broadcast_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(message, actor):
        return
    assert actor is not None
    body = (message.text or "").strip()
    if not body:
        await message.answer("Напишите текст сообщения.")
        return
    if len(body) > TEXT_LIMIT:
        await message.answer(f"Слишком длинное: {len(body)} символов. Лимит — {TEXT_LIMIT}.")
        return
    await state.update_data(bc_text=body)
    await state.set_state(BroadcastSG.confirm)
    catalog, selected, people = await _load(state, repo, config)
    await message.answer(
        _confirm_text(body, selected, catalog, people, actor.telegram_id),
        reply_markup=_confirm_kb(),
    )


@router.callback_query(F.data == "bc:ed")
async def edit_broadcast_text(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    catalog, selected, people = await _load(state, repo, config)
    count = len(broadcast_recipients(people, selected, catalog, exclude=actor.telegram_id))
    if count == 0:
        await _show_audience(cb, state, repo, config, actor)
        return
    await state.set_state(BroadcastSG.text)
    await respond(
        cb,
        f"📢 Напишите текст заново. Его получат <b>{count}</b>.",
        _prompt_kb(),
    )


@router.callback_query(F.data == "bc:ok")
async def send_broadcast(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, bot: Bot, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    data = await state.get_data()
    body = data.get("bc_text")
    if not isinstance(body, str) or not body.strip():
        await answer_callback(cb, "Сначала напишите текст", show_alert=True)
        return
    catalog, selected, people = await _load(state, repo, config)
    recipients = broadcast_recipients(people, selected, catalog, exclude=actor.telegram_id)
    if not recipients:
        await answer_callback(cb, "Кроме вас в выбранных группах никого нет", show_alert=True)
        return
    labels = selected_labels(selected, catalog)
    await answer_callback(cb, "Отправляю…")
    text = outgoing_text(actor, body)
    sent = blocked = failed = 0
    for chat_id in recipients:
        result = await _deliver(bot, chat_id, text)
        if result == "sent":
            sent += 1
        elif result == "blocked":
            blocked += 1
        else:
            failed += 1
    await audit(
        repo,
        actor,
        "broadcast.send",
        кому=", ".join(labels),
        получателей=len(recipients),
        доставлено=sent,
        не_доставлено=blocked + failed,
        текст=truncate(body, 200),
    )
    logger.info("Broadcast from %s delivered %s of %s", actor.telegram_id, sent, len(recipients))
    await state.clear()
    lines = ["📢 <b>Сообщение отправлено</b>", "", f"Доставлено: <b>{sent}</b> из {len(recipients)}"]
    missed = blocked + failed
    if missed:
        lines.append(f"Не доставлено: <b>{missed}</b>")
    await safe_edit(cb.message, "\n".join(lines), back_kb())


async def _deliver(bot: Bot, chat_id: int, text: str) -> str:
    for attempt in (1, 2):
        try:
            await bot.send_message(chat_id, text)
            return "sent"
        except TelegramRetryAfter as exc:
            if attempt == 2 or exc.retry_after > _FLOOD_WAIT_LIMIT:
                logger.warning("Broadcast flood wait for %s: %s", chat_id, exc.retry_after)
                return "failed"
            await asyncio.sleep(float(exc.retry_after))
        except TelegramForbiddenError:
            logger.info("Broadcast skipped %s: bot blocked", chat_id)
            return "blocked"
        except Exception:
            logger.warning("Broadcast to %s failed", chat_id, exc_info=True)
            return "failed"
    return "failed"
