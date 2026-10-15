"""Audit log viewer for markers and above."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.queries import Repo
from handlers.common import require_level, respond
from keyboards.main import nav_row, pager_row
from services.audit import format_audit_entry
from services.roles import Actor
from utils.callbacks import SEC_AUDIT

router = Router(name="audit")

PAGE = 10


async def show_audit(cb: CallbackQuery, repo: Repo, config: Config, page: int) -> None:
    total = await repo.count_audit()
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    entries = await repo.list_audit(limit=PAGE, offset=page * PAGE)
    body = [format_audit_entry(e, config.timezone) for e in entries] or ["Записей пока нет."]
    text = f"🕵️ <b>Журнал действий</b> · всего {total}\n\n" + "\n\n".join(body)
    if len(text) > 4000:
        text = text[:4000] + "…"
    b = InlineKeyboardBuilder()
    if pages > 1:
        b.row(*pager_row("aud", page, pages))
    b.row(*nav_row())
    await respond(cb, text, b.as_markup())


@router.callback_query(F.data == SEC_AUDIT)
async def open_audit(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_audit(cb, repo, config, 0)


@router.callback_query(F.data.startswith("aud:"))
async def audit_page(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await show_audit(cb, repo, config, int(cb.data.split(":", 1)[1]))
