"""How many people wrote to the bot, and how many of them are verified. Admins and above."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import BotUsage
from database.queries import Repo
from handlers.common import require_level, respond
from keyboards.main import nav_row
from services.roles import LEVEL_ADMIN, Actor
from utils.callbacks import SEC_USAGE

router = Router(name="usage")


def usage_text(stats: BotUsage) -> str:
    return "\n".join(
        [
            "📈 <b>Статистика бота</b>",
            "",
            f"Пользовались ботом: <b>{stats.total}</b>",
            f"Верифицированы: <b>{stats.verified}</b>",
            f"Не верифицированы: <b>{stats.unverified}</b>",
            "",
            "Считается каждый Telegram-аккаунт, который хотя бы раз написал боту. "
            "Верифицирован — аккаунт привязан к карточке студента.",
        ]
    )


@router.callback_query(F.data == SEC_USAGE)
async def open_usage(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_ADMIN):
        return
    await state.clear()
    b = InlineKeyboardBuilder()
    b.row(*nav_row())
    await respond(cb, usage_text(await repo.bot_usage()), b.as_markup())
