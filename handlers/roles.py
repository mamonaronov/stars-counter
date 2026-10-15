"""Role management: assign, revoke, hand over chief roles."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.queries import Repo
from handlers.common import PICK_FILTER_ONLY_VERIFIED, require_level, respond, show_main, show_student_picker
from keyboards.main import btn, confirm_kb, nav_row
from services.audit import audit
from services.roles import (
    ASSIGNABLE_ROLES,
    LEVEL_CHIEF_MARKER,
    ROLE_TITLES,
    Actor,
    role_after_transfer,
    role_level,
    role_title,
)
from utils.callbacks import SEC_ROLES
from utils.formatting import esc
from utils.telegram import answer_callback, safe_send_message

router = Router(name="roles")

PICK_CTX = "rol"
TRANSFER_CTX = "trf"


async def show_roles_home(target: CallbackQuery, repo: Repo, config: Config, actor: Actor) -> None:
    holders = await repo.list_students_with_roles()
    lines = ["🎖 <b>Роли</b>", ""]
    lines.append("1. суперадмин — задаётся в .env")
    for role in reversed(ASSIGNABLE_ROLES):
        names = [esc(s.full_name) for s in holders if s.role == role]
        lines.append(f"{6 - role_level(role)}. {ROLE_TITLES[role]} — {', '.join(names) if names else '—'}")
    lines.append("")
    lines.append(f"Ваш уровень: <b>{esc(actor.role_title)}</b>. Можно назначать: " + (", ".join(ROLE_TITLES[r] for r in actor.assignable_roles()) or "—") + ".")
    b = InlineKeyboardBuilder()
    b.row(btn("➕ Назначить / изменить роль", "rolp"))
    if actor.transferable_role:
        b.row(btn(f"🔁 Передать роль «{ROLE_TITLES[actor.transferable_role]}»", "trfp"))
    b.row(*nav_row())
    await respond(target, "\n".join(lines), b.as_markup())


@router.callback_query(F.data == SEC_ROLES)
async def open_roles(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    await state.clear()
    await show_roles_home(cb, repo, config, actor)


@router.callback_query(F.data == "rolp")
async def roles_pick(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    await show_student_picker(
        cb,
        state,
        repo,
        ctx=PICK_CTX,
        title="🎖 <b>Роли</b>\nВыберите студента (только верифицированные):",
        back=SEC_ROLES,
        only=PICK_FILTER_ONLY_VERIFIED,
        exclude=[actor.student.id] if actor.student else [],
    )


async def show_role_card(cb: CallbackQuery, repo: Repo, actor: Actor, student_id: int, *, notice: str | None = None) -> None:
    student = await repo.get_student(student_id)
    if student is None or not student.is_active or not student.is_verified:
        await answer_callback(cb, "Студент не найден или не верифицирован", show_alert=True)
        return
    text = f"👤 <b>{esc(student.full_name)}</b>\nТекущая роль: <b>{role_title(student.role)}</b>"
    b = InlineKeyboardBuilder()
    if actor.can_change_role_of(student):
        for role in actor.assignable_roles():
            if role != student.role:
                b.row(btn(f"→ {ROLE_TITLES[role]}", f"rola:{student.id}:{role}"))
        if actor.can_revoke(student):
            b.row(btn("🚫 Снять роль", f"rolr:{student.id}"))
    else:
        text += "\n\nВы не можете менять роль этого человека: его уровень не ниже вашего."
    b.row(*nav_row("rolp"))
    await respond(cb, text, b.as_markup(), notice=notice)


@router.callback_query(F.data.startswith(f"pk:{PICK_CTX}:"))
async def pick_for_role(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    await state.clear()
    await show_role_card(cb, repo, actor, int(cb.data.rsplit(":", 1)[1]))


@router.callback_query(F.data.startswith("rola:"))
async def assign_role(cb: CallbackQuery, repo: Repo, bot: Bot, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    _, raw_id, role = cb.data.split(":", 2)
    student = await repo.get_student(int(raw_id))
    if student is None or not student.is_active or not student.is_verified:
        await answer_callback(cb, "Студент не найден или не верифицирован", show_alert=True)
        return
    if not actor.can_assign(role) or not actor.can_change_role_of(student):
        await answer_callback(cb, "Недостаточно прав для этой роли.", show_alert=True)
        return
    old = student.role
    await repo.set_role(student.id, role)
    await audit(repo, actor, "role.assign", entity_type="student", entity_id=student.id, студент=student.full_name, было=role_title(old) if old else "нет", стало=ROLE_TITLES[role])
    if student.telegram_id:
        await safe_send_message(bot, student.telegram_id, f"🎖 Вам назначена роль <b>{ROLE_TITLES[role]}</b> ({esc(actor.display_name)}). Нажмите /menu.")
    await show_role_card(cb, repo, actor, student.id, notice="Роль назначена")


@router.callback_query(F.data.startswith("rolr:"))
async def revoke_role(cb: CallbackQuery, repo: Repo, bot: Bot, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    student = await repo.get_student(int(cb.data.split(":", 1)[1]))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    if not actor.can_revoke(student):
        await answer_callback(cb, "Недостаточно прав.", show_alert=True)
        return
    old = student.role
    await repo.set_role(student.id, None)
    await audit(repo, actor, "role.revoke", entity_type="student", entity_id=student.id, студент=student.full_name, было=role_title(old))
    if student.telegram_id:
        await safe_send_message(bot, student.telegram_id, f"Роль «{role_title(old)}» снята ({esc(actor.display_name)}).")
    await show_role_card(cb, repo, actor, student.id, notice="Роль снята")


# --------------------------------------------------------------- transfer


@router.callback_query(F.data == "trfp")
async def transfer_pick(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if actor is None or not actor.transferable_role or actor.student is None:
        await answer_callback(cb, "У вас нет роли, которую можно передать.", show_alert=True)
        return
    await show_student_picker(
        cb,
        state,
        repo,
        ctx=TRANSFER_CTX,
        title=f"🔁 <b>Передача роли «{ROLE_TITLES[actor.transferable_role]}»</b>\nКому передать? После передачи у вас останется роль «{ROLE_TITLES[role_after_transfer(actor.transferable_role)]}».",
        back=SEC_ROLES,
        only=PICK_FILTER_ONLY_VERIFIED,
        exclude=[actor.student.id],
    )


@router.callback_query(F.data.startswith(f"pk:{TRANSFER_CTX}:"))
async def transfer_confirm(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if actor is None or not actor.transferable_role:
        await answer_callback(cb, "У вас нет роли, которую можно передать.", show_alert=True)
        return
    await state.clear()
    student = await repo.get_student(int(cb.data.rsplit(":", 1)[1]))
    if student is None or not actor.can_transfer_to(student):
        await answer_callback(cb, "Этому студенту передать роль нельзя.", show_alert=True)
        return
    await respond(
        cb,
        f"Передать роль <b>{ROLE_TITLES[actor.transferable_role]}</b> студенту <b>{esc(student.full_name)}</b>?\n"
        f"У вас останется роль «{ROLE_TITLES[role_after_transfer(actor.transferable_role)]}».",
        confirm_kb(f"trfc:{student.id}", SEC_ROLES, yes_text="🔁 Передать"),
    )


@router.callback_query(F.data.startswith("trfc:"))
async def transfer_do(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, bot: Bot, actor: Actor | None) -> None:
    if actor is None or not actor.transferable_role or actor.student is None:
        await answer_callback(cb, "У вас нет роли, которую можно передать.", show_alert=True)
        return
    student = await repo.get_student(int(cb.data.split(":", 1)[1]))
    if student is None or not actor.can_transfer_to(student):
        await answer_callback(cb, "Этому студенту передать роль нельзя.", show_alert=True)
        return
    role = actor.transferable_role
    await repo.set_role(student.id, role)
    await repo.set_role(actor.student.id, role_after_transfer(role))
    await audit(repo, actor, "role.transfer", entity_type="student", entity_id=student.id, роль=ROLE_TITLES[role], кому=student.full_name)
    if student.telegram_id:
        await safe_send_message(bot, student.telegram_id, f"🎖 {esc(actor.display_name)} передал(а) вам роль <b>{ROLE_TITLES[role]}</b>. Нажмите /menu.")
    from middlewares import build_actor

    new_actor = await build_actor(repo, config, actor.telegram_id)
    await show_main(cb, new_actor, config, repo, state, notice="Роль передана")
