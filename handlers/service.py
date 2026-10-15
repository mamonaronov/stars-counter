"""Superadmin service panel: status, manual backup, attendance table for the markers group."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import Config
from database.queries import Repo
from handlers.common import require_level, respond
from keyboards.main import btn, nav_row
from services.alerts import notify_superadmin
from services.audit import audit
from services.group_export import KEY_GROUP, KEY_SENT_AT, markers_chat_id, publish_markers_export
from services.jobs import reschedule_telegram_backup
from services.roles import LEVEL_SUPERADMIN, Actor
from services.telegram_backup import (
    backup_group_bound_text,
    backup_group_membership_action,
    backup_group_unbound_text,
    backup_interval_caption,
    clear_telegram_backup_chat,
    is_backup_group_chat,
    last_telegram_backup_at,
    membership_in_chat,
    send_telegram_backup,
    set_telegram_backup_chat,
    telegram_backup_chat,
)
from services.vpn_monitor import ping_status_line
from utils.callbacks import SEC_SERVICE
from utils.formatting import esc
from utils.telegram import answer_callback
from utils.time import format_dt_iso

logger = logging.getLogger(__name__)

router = Router(name="service")


def _backup_line(config: Config, chat_id: int | None, title: str | None, last_sent) -> str:
    when = format_dt_iso(last_sent.isoformat() if last_sent else None, config.timezone)
    if config.telegram_backup_interval_minutes <= 0:
        return f"Бэкап в группу: выключен. Последний: {when}"
    if chat_id is None:
        return f"Бэкап в группу: не привязана. В группе отправьте /backup_here. Последний: {when}"
    name = esc(title) if title else str(chat_id)
    return (
        f"Бэкап в группу «{name}»: {backup_interval_caption(config.telegram_backup_interval_minutes)}. "
        f"Последний: {when}"
    )


async def show_service(cb: CallbackQuery, repo: Repo, config: Config) -> None:
    counts = await repo.counts()
    version = await repo.db.user_version()
    last_backup = await repo.db.get_system("last_backup_at")
    last_tg = await last_telegram_backup_at(repo.db)
    last_table = await repo.db.get_system(KEY_SENT_AT)
    backup_chat, backup_title = await telegram_backup_chat(repo.db)
    chat_id = await markers_chat_id(repo.db, config)
    if chat_id is None:
        table_line = "Таблица в группе отмечающих: не подключена. В группе отправьте /bindgroup"
    else:
        every = (
            f", проверка каждые {config.markers_export_interval_minutes} мин"
            if config.markers_export_interval_minutes > 0
            else ", автообновление выключено"
        )
        table_line = (
            f"Таблица в группе отмечающих ({chat_id}): {format_dt_iso(last_table, config.timezone)}{every}"
        )
    lines = [
        "🛠 <b>Сервис</b>",
        "",
        f"Студентов: {counts['students']} (верифицировано {counts['verified']})",
        f"Занятий: {counts['events']} · отметок: {counts['attendance']}",
        f"Записей в журнале: {counts['audit']} · Telegram-аккаунтов: {counts['users']}",
        f"Версия БД: {version} · часовой пояс: {config.timezone}",
        f"Последний бэкап на диск: {format_dt_iso(last_backup, config.timezone)}",
        _backup_line(config, backup_chat, backup_title, last_tg),
        table_line,
        await ping_status_line(repo, config),
    ]
    b = InlineKeyboardBuilder()
    b.row(btn("📦 Прислать бэкап сейчас", "srvb"))
    if chat_id is not None:
        b.row(btn("📤 Отправить таблицу в группу", "srvg"))
    b.row(*nav_row())
    await respond(cb, "\n".join(lines), b.as_markup())


@router.callback_query(F.data == SEC_SERVICE)
async def open_service(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    await state.clear()
    await show_service(cb, repo, config)


@router.callback_query(F.data == "srvb")
async def manual_backup(
    cb: CallbackQuery,
    repo: Repo,
    config: Config,
    bot: Bot,
    actor: Actor | None,
    scheduler: AsyncIOScheduler | None = None,
) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    assert actor is not None
    await answer_callback(cb, "Готовлю копию…")
    try:
        await send_telegram_backup(repo.db, bot, config, silent=False, chat_id=cb.from_user.id, force=True)
    except Exception:
        logger.exception("Manual telegram backup failed")
        await cb.message.answer("Не удалось собрать или отправить бэкап.")
        return
    if scheduler is not None:
        try:
            reschedule_telegram_backup(scheduler, bot, repo.db, config, await last_telegram_backup_at(repo.db))
        except Exception:
            logger.exception("Failed to reschedule telegram backup after manual send")
    await audit(repo, actor, "backup.manual")
    await show_service(cb, repo, config)


@router.callback_query(F.data == "srvg")
async def manual_markers_export(cb: CallbackQuery, repo: Repo, config: Config, bot: Bot, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    assert actor is not None
    if await markers_chat_id(repo.db, config) is None:
        await answer_callback(cb, "Группа не подключена. В ней отправьте /bindgroup", show_alert=True)
        return
    await answer_callback(cb, "Отправляю таблицу…")
    try:
        result = await publish_markers_export(bot, repo, config, silent=False, force=True)
    except Exception:
        logger.exception("Manual markers export failed")
        await cb.message.answer("Не удалось отправить таблицу в группу. Проверьте, что бот в группе и может писать сообщения.")
        return
    if result == "empty":
        await cb.message.answer("Занятий пока нет — отправлять нечего.")
        return
    if result != "sent":
        await cb.message.answer("Не удалось отправить таблицу в группу. Проверьте, что бот в группе и может писать сообщения.")
        return
    await audit(repo, actor, "export.markers")
    await show_service(cb, repo, config)


def _is_superadmin(actor: Actor | None) -> bool:
    return actor is not None and actor.is_superadmin


def _is_owner(user, config: Config) -> bool:
    return user is not None and user.id == config.superadmin_id


async def _bind_backup_group(
    repo: Repo,
    config: Config,
    bot: Bot,
    scheduler: AsyncIOScheduler | None,
    chat_id: int,
    title: str | None,
    actor: Actor | None,
) -> str:
    await set_telegram_backup_chat(repo.db, chat_id, title)
    if actor is not None:
        await audit(repo, actor, "backup.bind", чат=chat_id)
    if scheduler is not None:
        try:
            reschedule_telegram_backup(scheduler, bot, repo.db, config, await last_telegram_backup_at(repo.db))
        except Exception:
            logger.exception("Failed to reschedule telegram backup after group bind")
    return backup_group_bound_text(title, config.telegram_backup_interval_minutes)


@router.my_chat_member()
async def backup_chat_member_update(
    event: ChatMemberUpdated,
    config: Config,
    repo: Repo,
    bot: Bot,
    actor: Actor | None = None,
    scheduler: AsyncIOScheduler | None = None,
) -> None:
    stored_id, stored_title = await telegram_backup_chat(repo.db)
    action = backup_group_membership_action(
        event.chat.type,
        chat_id=event.chat.id,
        stored_id=stored_id,
        old_in=membership_in_chat(event.old_chat_member),
        new_in=membership_in_chat(event.new_chat_member),
        actor_is_owner=_is_owner(event.from_user, config),
    )
    if action == "bind":
        text = await _bind_backup_group(repo, config, bot, scheduler, event.chat.id, event.chat.title, actor)
        try:
            await event.answer(text)
        except Exception:
            logger.exception("Failed to announce backup group bind in %s", event.chat.id)
        return
    if action == "unbind":
        await clear_telegram_backup_chat(repo.db)
        if actor is not None:
            await audit(repo, actor, "backup.unbind", чат=event.chat.id)
        await notify_superadmin(bot, config, backup_group_unbound_text(stored_title or event.chat.title), silent=True)


@router.message(Command("backup_here"))
async def backup_here(
    message: Message,
    repo: Repo,
    config: Config,
    bot: Bot,
    actor: Actor | None = None,
    scheduler: AsyncIOScheduler | None = None,
) -> None:
    if not _is_owner(message.from_user, config):
        if message.chat.type == "private":
            await message.answer("Недостаточно прав для этого действия.")
        return
    if message.chat is None or not is_backup_group_chat(message.chat.type):
        await message.answer(
            "Добавьте бота в группу и напишите там /backup_here — "
            "автоматические бэкапы будут уходить в эту группу. "
            "В личку архив приходит только по кнопке в сервисе."
        )
        return
    text = await _bind_backup_group(repo, config, bot, scheduler, message.chat.id, message.chat.title, actor)
    await message.answer(text)


@router.message(Command("bindgroup"))
async def bind_markers_group(message: Message, repo: Repo, config: Config, bot: Bot, actor: Actor | None) -> None:
    if not _is_superadmin(actor):
        await message.answer("Недостаточно прав для этого действия.")
        return
    assert actor is not None
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer(
            "Добавьте бота в группу отмечающих и отправьте /bindgroup там. "
            "Боту нужно право писать сообщения и закреплять их."
        )
        return
    await repo.db.set_system(KEY_GROUP, str(message.chat.id))
    await audit(repo, actor, "export.bind", чат=message.chat.id)
    try:
        result = await publish_markers_export(bot, repo, config, silent=False, force=True)
    except Exception:
        logger.exception("Initial markers export failed")
        await message.answer(
            "Группа сохранена, но таблицу отправить не удалось. "
            "Дайте боту право писать в группу и нажмите в сервисе «Отправить таблицу в группу»."
        )
        return
    if result == "empty":
        await message.answer("Группа подключена. Занятий пока нет — таблица появится вместе с первым занятием.")
        return
    if result != "sent":
        await message.answer(
            "Группа сохранена, но таблицу отправить не удалось. "
            "Дайте боту право писать в группу и нажмите в сервисе «Отправить таблицу в группу»."
        )
        return
    await message.answer(
        "Группа подключена. Таблица посещаемости закреплена здесь и будет обновляться, когда данные меняются."
    )


@router.message(Command("unbindgroup"))
async def unbind_markers_group(message: Message, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not _is_superadmin(actor):
        await message.answer("Недостаточно прав для этого действия.")
        return
    assert actor is not None
    await repo.db.set_system(KEY_GROUP, "")
    await audit(repo, actor, "export.unbind")
    if config.markers_group_id is not None:
        await message.answer("Сохранённая группа сброшена. Снова используется MARKERS_GROUP_ID из настроек.")
        return
    await message.answer("Группа для таблицы отключена. Автоотправка остановлена.")
