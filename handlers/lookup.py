"""Look up a student's name by Telegram nick, and the nick by the name."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database.models import Student, User
from database.queries import Repo
from handlers.common import require_level, respond
from keyboards.main import back_kb
from services.roles import LEVEL_MARKER, Actor
from states import LookupSG
from utils.callbacks import SEC_LOOKUP
from utils.formatting import esc

router = Router(name="lookup")

_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(@?[A-Za-z0-9_]+)/?$", re.IGNORECASE)
_NICK = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_LIMIT = 15

PROMPT = (
    "🔎 <b>Ник и ФИО</b>\n\n"
    "Напишите ник или ФИО.\n"
    "Ник: <code>@username</code>\n"
    "ФИО: фамилия или фамилия и имя.\n\n"
    "Можно сразу прислать следующий запрос. Ник известен, если человек хотя бы раз писал боту."
)


def _kind(text: str) -> tuple[str, str]:
    raw = " ".join(text.strip().split())
    link = _LINK.fullmatch(raw)
    if link:
        return "nick", link.group(1).lstrip("@")
    if raw.startswith("@"):
        return "nick", raw[1:].strip()
    if _NICK.fullmatch(raw):
        return "maybe_nick", raw
    return "name", raw


def _nick_of(user: User | None) -> str:
    if user is None or not user.username:
        return "ник не известен"
    return f"@{esc(user.username)}"


async def lookup_text(repo: Repo, text: str) -> str:
    kind, value = _kind(text)
    if not value:
        return "Напишите ник или ФИО."
    if kind in {"nick", "maybe_nick"}:
        users = await repo.find_users_by_username(value)
        if users:
            return await _by_nick(repo, users)
        if kind == "nick":
            return (
                f"Ник @{esc(value)} не найден.\n"
                "Бот запоминает ник, когда человек пишет ему."
            )
    if len(value) < 2:
        return "Для поиска по ФИО напишите хотя бы две буквы."
    return await _by_name(repo, value)


async def _by_nick(repo: Repo, users: list[User]) -> str:
    lines = ["🔎 <b>По нику</b>", ""]
    for user in users:
        nick = f"@{esc(user.username)}" if user.username else str(user.telegram_id)
        student = await repo.get_student_by_telegram(user.telegram_id)
        if student is None:
            lines.append(f"{nick} — к карточке студента не привязан")
            continue
        lines.append(f"{nick} — <b>{esc(student.full_name)}</b>{_removed(student)}")
    lines.append("")
    lines.append("Пришлите ещё ник или ФИО.")
    return "\n".join(lines)


async def _by_name(repo: Repo, query: str) -> str:
    found = await repo.list_students(active_only=False, query=query)
    needle = " ".join(query.split()).casefold()
    exact = [student for student in found if " ".join(student.full_name.split()).casefold() == needle]
    shown = exact or found
    if not shown:
        return f"Студента «{esc(query)}» нет."
    hidden = max(0, len(shown) - _LIMIT)
    lines = ["🔎 <b>По ФИО</b>", ""]
    for student in shown[:_LIMIT]:
        user = await repo.get_user(student.telegram_id) if student.telegram_id else None
        if student.telegram_id is None:
            nick = "Telegram не привязан"
        else:
            nick = _nick_of(user)
        lines.append(f"<b>{esc(student.full_name)}</b>{_removed(student)} — {nick}")
    if hidden:
        lines.append(f"Ещё {hidden}. Уточните ФИО.")
    lines.append("")
    lines.append("Пришлите ещё ник или ФИО.")
    return "\n".join(lines)


def _removed(student: Student) -> str:
    return "" if student.is_active else " · удалён"


@router.callback_query(F.data == SEC_LOOKUP)
async def open_lookup(cb: CallbackQuery, state: FSMContext, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_MARKER):
        return
    await state.set_state(LookupSG.query)
    await respond(cb, PROMPT, back_kb())


@router.message(LookupSG.query, F.text)
async def lookup_query(message: Message, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(message, actor, LEVEL_MARKER):
        await state.clear()
        return
    await message.answer(await lookup_text(repo, message.text or ""), reply_markup=back_kb())
