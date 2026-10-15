"""Last router: anything not handled above."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import Config
from database.queries import Repo
from handlers.common import show_main
from services.roles import Actor
from utils.telegram import answer_callback

router = Router(name="fallback")


@router.message(F.text)
async def unknown_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    current = await state.get_state()
    if current is not None:
        await message.answer("Жду другой ввод. Нажмите кнопку на экране выше или /menu, чтобы начать заново.")
        return
    await show_main(message, actor, config, repo)


@router.callback_query()
async def unknown_callback(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    await answer_callback(cb, "Эта кнопка устарела. Открываю меню.")
    await show_main(cb, actor, config, repo, state)
