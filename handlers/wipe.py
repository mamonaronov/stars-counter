"""Erase stored data so the bot looks freshly installed. The config file stays."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import Config
from database.queries import Repo
from handlers.common import require_level, respond, show_main
from keyboards.main import cancel_kb, confirm_kb
from middlewares import build_actor
from services.alerts import notify_superadmin
from services.roles import LEVEL_CHIEF_ADMIN, Actor
from states import WipeSG
from utils.callbacks import NAV_CANCEL, SEC_WIPE, WIPE_ASK, WIPE_CONFIRM
from utils.formatting import esc
from utils.telegram import answer_callback

logger = logging.getLogger(__name__)

router = Router(name="wipe")

CONFIRM_WORD = "стереть"

_FIRST = (
    "⚠️ <b>Очистить все данные</b>\n"
    "\n"
    "Это сотрёт всё, что бот накопил, как будто его только что запустили:\n"
    "• студенты, роли и привязки Telegram\n"
    "• занятия, отметки и шаблоны\n"
    "• больничные, отпросы и запросы\n"
    "• журнал действий\n"
    "• привязки групп, сохранённые в базе\n"
    "• резервные копии на диске и история проверок связи\n"
    "\n"
    "Файл настроек не изменится: токен, суперадмин, часовой пояс и пути останутся как были.\n"
    "\n"
    "Дальше будет ещё одно предупреждение, затем нужно вписать слово."
)

_SECOND = (
    "⚠️ <b>Данные пропадут безвозвратно</b>\n"
    "\n"
    "Восстановить их из бота будет нельзя.\n"
    "Роли сбросятся: останется только суперадмин из настроек. "
    "Главный админ после этого снова станет обычным пользователем.\n"
    "Группу отмечающих и группу бэкапов придётся привязать заново, если они были заданы в боте.\n"
    "\n"
    "Если передумаете — нажмите «Отмена»."
)

_WORD = (
    "Последнее подтверждение.\n"
    "\n"
    "Напишите слово <code>СТЕРЕТЬ</code> одним сообщением, без кавычек. Регистр не важен.\n"
    "Пока слово не совпадёт, ничего не удалится."
)


def _word_ok(text: str) -> bool:
    return text.strip().casefold().replace("ё", "е") == CONFIRM_WORD


@router.callback_query(F.data == SEC_WIPE)
async def open_wipe(cb: CallbackQuery, state: FSMContext, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_ADMIN):
        return
    await state.clear()
    await respond(cb, _FIRST, confirm_kb(WIPE_CONFIRM, NAV_CANCEL, yes_text="Я понимаю, продолжить"))


@router.callback_query(F.data == WIPE_CONFIRM)
async def wipe_second_warning(cb: CallbackQuery, state: FSMContext, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_ADMIN):
        await state.clear()
        return
    await state.set_state(WipeSG.warned)
    await respond(cb, _SECOND, confirm_kb(WIPE_ASK, NAV_CANCEL, yes_text="Всё равно стереть"))


@router.callback_query(F.data == WIPE_ASK)
async def wipe_ask_word(cb: CallbackQuery, state: FSMContext, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_ADMIN):
        await state.clear()
        return
    if await state.get_state() != WipeSG.warned.state:
        await answer_callback(cb, "Сначала прочитайте первое предупреждение.", show_alert=True)
        await state.clear()
        await respond(cb, _FIRST, confirm_kb(WIPE_CONFIRM, NAV_CANCEL, yes_text="Я понимаю, продолжить"))
        return
    await state.set_state(WipeSG.word)
    await respond(cb, _WORD, cancel_kb())


@router.message(WipeSG.word, F.text)
async def wipe_by_word(
    message: Message,
    state: FSMContext,
    repo: Repo,
    config: Config,
    bot: Bot,
    actor: Actor | None,
) -> None:
    if not await require_level(message, actor, LEVEL_CHIEF_ADMIN):
        await state.clear()
        return
    assert actor is not None
    if not _word_ok(message.text or ""):
        await message.answer(
            "Это не то слово. Чтобы стереть данные, напишите <code>СТЕРЕТЬ</code>.\nИли нажмите «Отмена».",
            reply_markup=cancel_kb(),
        )
        return
    logger.warning("Wiping all runtime data telegram_id=%s role=%s", actor.telegram_id, actor.role_name)
    try:
        await repo.db.wipe_all_data()
    except Exception:
        logger.exception("Wipe failed for telegram_id=%s", actor.telegram_id)
        await message.answer(
            "Не удалось закончить очистку. Отправьте слово <code>СТЕРЕТЬ</code> ещё раз. Файл настроек не менялся.",
            reply_markup=cancel_kb(),
        )
        return
    if not actor.is_superadmin:
        await notify_superadmin(
            bot,
            config,
            "⚠️ <b>Все данные бота стёрты</b>\n"
            f"Это сделал {esc(actor.display_name)} (id {actor.telegram_id}), роль: {esc(actor.role_title)}.\n"
            "Файл настроек не менялся. Бот снова как после первого запуска.",
        )
    fresh = await build_actor(repo, config, actor.telegram_id)
    done = ["✅ <b>Все данные удалены.</b>", "Бот в состоянии первого запуска. Файл настроек не менялся."]
    if not fresh.is_superadmin:
        done.append("Ваша роль тоже сброшена. Снова очистить данные может только суперадмин.")
    await message.answer("\n".join(done))
    await show_main(message, fresh, config, repo, state)
