"""Superadmin service panel: status and manual backup."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.queries import Repo
from handlers.common import require_level, respond
from keyboards.main import btn, nav_row
from services.audit import audit
from services.jobs import send_db_snapshot
from services.roles import LEVEL_SUPERADMIN, Actor
from utils.callbacks import SEC_SERVICE
from utils.telegram import answer_callback
from utils.time import format_dt_iso

router = Router(name="service")


async def show_service(cb: CallbackQuery, repo: Repo, config: Config) -> None:
    counts = await repo.counts()
    version = await repo.db.user_version()
    last_backup = await repo.db.get_system("last_backup_at")
    last_tg = await repo.db.get_system("last_telegram_backup_at")
    lines = [
        "🛠 <b>Сервис</b>",
        "",
        f"Студентов: {counts['students']} (верифицировано {counts['verified']})",
        f"Занятий: {counts['events']} · отметок: {counts['attendance']}",
        f"Записей в журнале: {counts['audit']} · Telegram-аккаунтов: {counts['users']}",
        f"Версия БД: {version} · часовой пояс: {config.timezone}",
        f"Последний бэкап на диск: {format_dt_iso(last_backup, config.timezone)}",
        f"Последний бэкап в Telegram: {format_dt_iso(last_tg, config.timezone)}",
    ]
    b = InlineKeyboardBuilder()
    b.row(btn("📦 Прислать бэкап сейчас", "srvb"))
    b.row(*nav_row())
    await respond(cb, "\n".join(lines), b.as_markup())


@router.callback_query(F.data == SEC_SERVICE)
async def open_service(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    await state.clear()
    await show_service(cb, repo, config)


@router.callback_query(F.data == "srvb")
async def manual_backup(cb: CallbackQuery, repo: Repo, config: Config, bot: Bot, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    assert actor is not None
    await answer_callback(cb, "Готовлю копию…")
    await send_db_snapshot(repo.db, bot, config, cb.from_user.id, silent=False)
    await audit(repo, actor, "backup.manual")
    await show_service(cb, repo, config)
