"""Shared event templates for quick event creation."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.queries import Repo
from handlers.common import KIND_TITLES, require_level, respond
from keyboards.main import btn, cancel_kb, confirm_kb, kind_kb, nav_row
from services.audit import audit
from services.roles import Actor
from states import TemplateSG
from utils.callbacks import SEC_TEMPLATES
from utils.formatting import esc, truncate
from utils.telegram import answer_callback
from utils.time import parse_hhmm

router = Router(name="templates")


async def show_templates(target: CallbackQuery | Message, repo: Repo) -> None:
    templates = await repo.list_templates()
    lines = ["📋 <b>Шаблоны занятий</b>", "", "Шаблон задаёт название, тип и время по умолчанию. Общие для всех отмечающих."]
    b = InlineKeyboardBuilder()
    for tpl in templates:
        kind = "📘" if tpl.kind == "mandatory" else "🟢"
        time = f" · {tpl.default_time}" if tpl.default_time else ""
        b.row(btn(f"{kind} {truncate(tpl.title, 30)}{time}", f"tpl:{tpl.id}"))
    if not templates:
        lines.append("\nШаблонов пока нет.")
    b.row(btn("➕ Новый шаблон", "tpln"))
    b.row(*nav_row())
    await respond(target, "\n".join(lines), b.as_markup())


@router.callback_query(F.data == SEC_TEMPLATES)
async def open_templates(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_templates(cb, repo)


@router.callback_query(F.data == "tpln")
async def new_template(cb: CallbackQuery, state: FSMContext, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.set_state(TemplateSG.title)
    await respond(cb, "Название шаблона? Например: <i>Лекция по истории</i>.", cancel_kb(SEC_TEMPLATES))


@router.message(TemplateSG.title, F.text)
async def new_template_title(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())[:80]
    if not title:
        await message.answer("Название не может быть пустым.", reply_markup=cancel_kb(SEC_TEMPLATES))
        return
    await state.update_data(tpl_title=title)
    await state.set_state(TemplateSG.kind)
    await message.answer(f"Название: <b>{esc(title)}</b>\n\nТип занятия?", reply_markup=kind_kb("tk", back=SEC_TEMPLATES))


@router.callback_query(TemplateSG.kind, F.data.startswith("tk:"))
async def new_template_kind(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(tpl_kind=cb.data.split(":", 1)[1])
    await state.set_state(TemplateSG.time)
    b = InlineKeyboardBuilder()
    b.row(btn("⏭ Без времени", "tt:none"))
    b.row(*nav_row(SEC_TEMPLATES))
    await respond(cb, "Время начала по умолчанию? Напишите, например <code>09:00</code>, или пропустите.", b.as_markup())


async def _finish_template(target: CallbackQuery | Message, state: FSMContext, repo: Repo, actor: Actor, default_time: str | None) -> None:
    if not await require_level(target, actor):
        await state.clear()
        return
    data = await state.get_data()
    tpl = await repo.create_template(data["tpl_title"], data["tpl_kind"], default_time, actor.telegram_id)
    await state.clear()
    await audit(repo, actor, "template.create", entity_type="template", entity_id=tpl.id, шаблон=tpl.title, тип=KIND_TITLES[tpl.kind], время=default_time)
    await show_templates(target, repo)


@router.callback_query(TemplateSG.time, F.data == "tt:none")
async def new_template_no_time(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor) -> None:
    await _finish_template(cb, state, repo, actor, None)


@router.message(TemplateSG.time, F.text)
async def new_template_time(message: Message, state: FSMContext, repo: Repo, actor: Actor) -> None:
    try:
        value = parse_hhmm(message.text or "")
    except ValueError:
        await message.answer("Не понял время. Пример: <code>09:00</code>.")
        return
    await _finish_template(message, state, repo, actor, value)


async def show_template_card(cb: CallbackQuery, repo: Repo, template_id: int, *, notice: str | None = None) -> None:
    tpl = await repo.get_template(template_id)
    if tpl is None or tpl.archived_at:
        await answer_callback(cb, "Шаблон не найден", show_alert=True)
        return
    text = (
        f"📋 <b>{esc(tpl.title)}</b>\nТип: {KIND_TITLES[tpl.kind]}\n"
        f"Время по умолчанию: {tpl.default_time or 'не задано'}"
    )
    b = InlineKeyboardBuilder()
    b.row(btn("🔁 Сменить тип", f"tplk:{tpl.id}"), btn("🗑 Удалить", f"tpld:{tpl.id}"))
    b.row(*nav_row(SEC_TEMPLATES))
    await respond(cb, text, b.as_markup(), notice=notice)


@router.callback_query(F.data.startswith("tpl:"))
async def template_card(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    await state.clear()
    await show_template_card(cb, repo, int(cb.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("tplk:"))
async def template_toggle_kind(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    tpl = await repo.get_template(int(cb.data.split(":", 1)[1]))
    if tpl is None:
        await answer_callback(cb, "Шаблон не найден", show_alert=True)
        return
    new_kind = "free" if tpl.kind == "mandatory" else "mandatory"
    await repo.update_template(tpl.id, kind=new_kind)
    await audit(repo, actor, "template.update", entity_type="template", entity_id=tpl.id, шаблон=tpl.title, тип=KIND_TITLES[new_kind])
    await show_template_card(cb, repo, tpl.id, notice="Тип изменён")


@router.callback_query(F.data.startswith("tpld:"))
async def template_delete_ask(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    tpl = await repo.get_template(int(cb.data.split(":", 1)[1]))
    if tpl is None:
        await answer_callback(cb, "Шаблон не найден", show_alert=True)
        return
    await respond(cb, f"Удалить шаблон <b>{esc(tpl.title)}</b>? Уже созданные занятия не изменятся.", confirm_kb(f"tpldc:{tpl.id}", f"tpl:{tpl.id}", yes_text="🗑 Удалить"))


@router.callback_query(F.data.startswith("tpldc:"))
async def template_delete(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor):
        return
    assert actor is not None
    tpl = await repo.get_template(int(cb.data.split(":", 1)[1]))
    if tpl is None:
        await answer_callback(cb, "Шаблон не найден", show_alert=True)
        return
    await repo.archive_template(tpl.id)
    await audit(repo, actor, "template.delete", entity_type="template", entity_id=tpl.id, шаблон=tpl.title)
    await answer_callback(cb, "Шаблон удалён")
    await show_templates(cb, repo)
