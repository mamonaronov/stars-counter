"""/start (with verification deep link), /menu, /help, navigation."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import Config
from database.queries import Repo
from handlers.common import render_student_picker, show_main
from keyboards.main import back_kb
from middlewares import build_actor
from services.audit import audit
from services.roles import LEVEL_CHIEF_MARKER, LEVEL_MARKER, Actor
from states import PickSG
from utils.callbacks import NAV_CANCEL, NAV_MAIN, NOOP, SEC_HELP
from utils.formatting import esc
from utils.telegram import answer_callback

router = Router(name="start")

VERIFY_PREFIX = "v_"


async def _verify(message: Message, token: str, repo: Repo, config: Config, actor: Actor) -> str:
    link = await repo.get_verification_link(token)
    if link is None:
        return "Ссылка не найдена. Попросите отмечающего прислать новую."
    if link.revoked_at or link.used_at:
        return "Эта ссылка уже использована или отозвана. Попросите отмечающего прислать новую."
    student = await repo.get_student(link.student_id)
    if student is None or not student.is_active:
        return "Студент по этой ссылке больше не числится в группе."
    if student.telegram_id is not None and student.telegram_id != actor.telegram_id:
        return "К этому студенту уже привязан другой Telegram-аккаунт. Обратитесь к отмечающему."
    if actor.student is not None and actor.student.id != student.id:
        if not actor.student.is_active:
            return (
                f"Ваш Telegram привязан к удалённой карточке «{esc(actor.student.full_name)}». "
                "Попросите восстановить её: новая карточка не получит старую историю посещений."
            )
        return (
            f"Ваш аккаунт уже привязан к студенту «{esc(actor.student.full_name)}». "
            "Чтобы перепривязать, обратитесь к отмечающему."
        )
    await repo.bind_telegram(student.id, actor.telegram_id)
    await repo.use_verification_link(token, actor.telegram_id)
    new_actor = await build_actor(repo, config, actor.telegram_id)
    await audit(
        repo,
        new_actor,
        "verification.done",
        entity_type="student",
        entity_id=student.id,
        студент=student.full_name,
        telegram_id=actor.telegram_id,
    )
    return f"✅ Готово! Вы подтверждены как <b>{esc(student.full_name)}</b>."


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    repo: Repo,
    config: Config,
    actor: Actor | None,
) -> None:
    await state.clear()
    payload = (command.args or "").strip()
    if payload.startswith(VERIFY_PREFIX) and actor is not None:
        notice = await _verify(message, payload[len(VERIFY_PREFIX) :], repo, config, actor)
        await message.answer(notice)
        actor = await build_actor(repo, config, actor.telegram_id)
    await show_main(message, actor, config, repo)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    await show_main(message, actor, config, repo, state)


def help_text(actor: Actor | None) -> str:
    level = actor.level if actor else 0
    parts = [
        "ℹ️ <b>Как пользоваться ботом</b>",
        "",
        "• <b>Посещаемость студентов</b> — выберите студента и месяц, чтобы увидеть процент и список занятий. Доступно всем.",
        "• Процент = (посещённые обязательные + посещённые свободные + пропуски по уважительной причине) / все обязательные занятия периода. "
        "Свободные занятия, на которые студент не пришёл, не учитываются, поэтому процент может быть выше 100.",
    ]
    if actor is not None and actor.is_linked:
        parts += [
            "• <b>Моя посещаемость</b> — ваша статистика за любой месяц.",
            "• <b>Запрос отмечающему</b> — если вас забыли отметить: выберите занятие и напишите сообщение.",
        ]
    else:
        parts.append("• Чтобы бот знал, кто вы, попросите у отмечающего одноразовую ссылку верификации и перейдите по ней.")
    if level >= LEVEL_MARKER:
        parts += [
            "",
            "<b>Отмечающему</b>",
            "• <b>Новое занятие</b> — из шаблона или вручную, на сегодня или задним числом. После создания откроется экран отметки: нажмите на студента — появится ✅, повторно — снимется.",
            "• <b>Занятия</b> — все занятия, можно открыть любое и поменять отметки, название, дату, тип.",
            "• <b>Месяцы и отчёты</b> — месяц заканчивается тем занятием, которое вы отметите как последнее. Там же Excel-отчёт (закрытый или промежуточный).",
            "• <b>Запросы</b> — сообщения студентов «меня забыли отметить»: отметить или отклонить.",
            "• <b>Больничные и отпросы</b> — пропуски за период считаются посещёнными; можно отменить.",
            "• <b>Верификация</b> — одноразовая ссылка для студента, перешлите её ему.",
            "• <b>Журнал действий</b> — кто, когда и с какими правами что сделал.",
        ]
    if level >= LEVEL_CHIEF_MARKER:
        parts += [
            "",
            "<b>Главному отмечающему и выше</b>",
            "• <b>Студенты</b> — добавить по одному или списком («Иван Иванов, 2023» в каждой строке), изменить, удалить.",
            "• <b>Роли</b> — назначить/снять роль тем, кто ниже вас; главные роли можно передать.",
        ]
    return "\n".join(parts)


@router.message(Command("help"))
async def cmd_help(message: Message, actor: Actor | None) -> None:
    await message.answer(help_text(actor), reply_markup=back_kb())


@router.callback_query(F.data == SEC_HELP)
async def cb_help(cb: CallbackQuery, actor: Actor | None) -> None:
    await answer_callback(cb)
    from utils.telegram import safe_edit

    await safe_edit(cb.message, help_text(actor), back_kb())


@router.callback_query(F.data.in_({NAV_MAIN, NAV_CANCEL}))
async def cb_main(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    await show_main(cb, actor, config, repo, state)


@router.callback_query(F.data == NOOP)
async def cb_noop(cb: CallbackQuery) -> None:
    await answer_callback(cb)


# ------------------------------------------------------- shared student picker


@router.callback_query(F.data.startswith("pkp:"))
async def cb_picker_page(cb: CallbackQuery, state: FSMContext, repo: Repo) -> None:
    _, ctx, page = cb.data.split(":", 2)
    data = await state.get_data()
    if data.get("pick_ctx") != ctx:
        await answer_callback(cb, "Список устарел, откройте раздел заново.", show_alert=True)
        return
    await render_student_picker(cb, state, repo, page=int(page))


@router.message(PickSG.filter, F.text)
async def picker_filter(message: Message, state: FSMContext, repo: Repo) -> None:
    query = (message.text or "").strip()
    await state.update_data(pick_query=query[:40] or None)
    await render_student_picker(message, state, repo, page=0)
