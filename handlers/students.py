"""Students: list, card, add one, bulk import, edit, remove/restore, verification links."""

from __future__ import annotations

from datetime import date

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database.models import Student
from database.queries import Repo
from handlers.common import (
    PICK_FILTER_ONLY_UNVERIFIED,
    require_level,
    respond,
    show_student_picker,
)
from keyboards.main import btn, cancel_kb, confirm_kb, fit_page, nav_row, pager_row
from services.audit import audit
from services.roles import LEVEL_CHIEF_MARKER, LEVEL_MARKER, Actor, role_title

PROTECTED_STUDENT = "Нельзя менять студента с такой же или более высокой ролью."
from services.students import (
    DIRECTIONS,
    DIRECTION_LIMIT,
    GROUP_LIMIT,
    MAX_IMPORT_LINES,
    TRACKS,
    TRACK_LIMIT,
    clean_profile_text,
    match_known,
    normalize_name,
    parse_enrollment_year,
    parse_student_lines,
)
from states import StudentSG
from utils.callbacks import NAV_MAIN, SEC_STUDENTS, SEC_VERIFY
from utils.formatting import course_text, esc, students_word
from utils.telegram import answer_callback
from utils.time import format_dt_iso, local_today, parse_date_text

router = Router(name="students")

PICK_CTX = "stu"
VERIFY_CTX = "ver"


# ---------------------------------------------------------------- section


async def show_students_home(target: CallbackQuery | Message, repo: Repo) -> None:
    counts = await repo.counts()
    text = (
        "👥 <b>Студенты</b>\n\n"
        f"В группе: <b>{counts['students']}</b>, верифицированы: <b>{counts['verified']}</b>."
    )
    b = InlineKeyboardBuilder()
    b.row(btn("📋 Список", "stul"), btn("➕ Добавить", "stua"))
    b.row(btn("📥 Импорт списком", "stui"), btn("🗂 Удалённые", "stur"))
    b.row(*nav_row())
    await respond(target, text, b.as_markup())


@router.callback_query(F.data == SEC_STUDENTS)
async def open_students(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await state.clear()
    await show_students_home(cb, repo)


@router.callback_query(F.data == "stul")
async def students_list(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await show_student_picker(cb, state, repo, ctx=PICK_CTX, title="👥 <b>Студенты</b>\nВыберите студента:", back=SEC_STUDENTS)


# ------------------------------------------------------------------ card


def _card_text(student: Student, config: Config, tg_display: str | None) -> str:
    today = local_today(config.timezone)
    lines = [
        f"👤 <b>{esc(student.full_name)}</b>",
        f"Курс: {course_text(student.enrollment_year, today.year, today.month)}",
        f"Направление: {esc(student.direction) if student.direction else '—'}",
        f"Молитва / тфилин: {esc(student.track) if student.track else '—'}",
        f"Группа иудаики: {esc(student.judaica_group) if student.judaica_group else '—'}",
        f"Роль: {role_title(student.role)}",
        f"Учитывается с: {student.joined_date.strftime('%d.%m.%Y')}",
    ]
    if student.is_verified:
        lines.append(f"Telegram: {esc(tg_display or student.telegram_id)} · подтверждён {format_dt_iso(student.verified_at, config.timezone)}")
    else:
        lines.append("Telegram: не привязан")
    if not student.is_active:
        lines.append(f"<b>Удалён</b> {student.left_date.strftime('%d.%m.%Y') if student.left_date else ''}")
    return "\n".join(lines)


async def show_student_card(target: CallbackQuery | Message, repo: Repo, config: Config, student_id: int, *, notice: str | None = None) -> None:
    student = await repo.get_student(student_id)
    if student is None:
        if isinstance(target, CallbackQuery):
            await answer_callback(target, "Студент не найден", show_alert=True)
        return
    tg_display = None
    if student.telegram_id:
        user = await repo.get_user(student.telegram_id)
        tg_display = user.display if user else None
    b = InlineKeyboardBuilder()
    if student.is_active:
        if not student.is_verified:
            b.row(btn("🔗 Ссылка верификации", f"verl:{student.id}"))
        b.row(btn("✏️ Имя", f"ste:name:{student.id}"), btn("✏️ Год поступления", f"ste:year:{student.id}"))
        b.row(btn("Направление", f"ste:direction:{student.id}"), btn("Группа иудаики", f"ste:group:{student.id}"))
        b.row(btn("Молитва / тфилин", f"ste:track:{student.id}"))
        b.row(btn("📅 Учитывать с даты", f"ste:joined:{student.id}"))
        if student.is_verified:
            b.row(btn("🔓 Отвязать Telegram", f"stunl:{student.id}"))
        b.row(btn("🗑 Удалить из группы", f"std:{student.id}"))
    else:
        b.row(btn("♻️ Восстановить", f"stre:{student.id}"))
    b.row(*nav_row("stul" if student.is_active else "stur"))
    await respond(target, _card_text(student, config, tg_display), b.as_markup(), notice=notice)


@router.callback_query(F.data.startswith(f"pk:{PICK_CTX}:"))
async def pick_student(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await state.clear()
    await show_student_card(cb, repo, config, int(cb.data.rsplit(":", 1)[1]))


@router.callback_query(F.data.startswith("stc:"))
async def student_card(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await state.clear()
    await show_student_card(cb, repo, config, int(cb.data.split(":", 1)[1]))


# ------------------------------------------------------------------- add


@router.callback_query(F.data == "stua")
async def add_student_start(cb: CallbackQuery, state: FSMContext, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await state.set_state(StudentSG.add_name)
    await respond(cb, "ФИО студента? Например: <i>Иванов Иван Иванович</i>.", cancel_kb(SEC_STUDENTS))


@router.message(StudentSG.add_name, F.text)
async def add_student_name(message: Message, state: FSMContext, repo: Repo) -> None:
    name = normalize_name(message.text or "")
    if len(name.split()) < 2:
        await message.answer("Нужны минимум фамилия и имя.", reply_markup=cancel_kb(SEC_STUDENTS))
        return
    existing = await repo.find_student_by_name(name)
    removed = None if existing else await repo.find_student_by_name(name, active_only=False)
    await state.update_data(new_name=name)
    if existing or (removed is not None and not removed.is_active):
        other = existing or removed
        assert other is not None
        b = InlineKeyboardBuilder()
        if existing:
            text = (
                f"Студент <b>{esc(name)}</b> уже есть в группе. "
                "Новая карточка не свяжется с его посещениями.\n\n"
                "Откройте существующую карточку или подтвердите, что это другой человек."
            )
            b.row(btn("Открыть карточку", f"stc:{other.id}"))
        else:
            text = (
                f"<b>{esc(name)}</b> есть среди удалённых. "
                "Новая карточка не получит старую историю посещений.\n\n"
                "Восстановите карточку или подтвердите, что это другой человек."
            )
            b.row(btn("♻️ Восстановить карточку", f"stre:{other.id}"))
        b.row(btn("Это другой человек", "stuf"))
        b.row(btn("✖️ Отмена", SEC_STUDENTS))
        await message.answer(text, reply_markup=b.as_markup())
        return
    await _ask_enrollment_year(message, state, name)


async def _ask_enrollment_year(target: CallbackQuery | Message, state: FSMContext, name: str) -> None:
    await state.update_data(new_name=name)
    await state.set_state(StudentSG.add_year)
    await respond(target, f"Имя: <b>{esc(name)}</b>\n\nГод поступления? Например: <code>2023</code>.", cancel_kb(SEC_STUDENTS))


@router.callback_query(StudentSG.add_name, F.data == "stuf")
async def add_student_force(cb: CallbackQuery, state: FSMContext, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    name = (await state.get_data()).get("new_name")
    if not name:
        await answer_callback(cb, "Имя не найдено, начните сначала", show_alert=True)
        return
    await _ask_enrollment_year(cb, state, str(name))


def _joined_prompt(today: date) -> str:
    return (
        "С какой даты учитывать занятия для статистики? Занятия раньше этой даты не войдут в процент.\n"
        f"Нажмите «Сегодня» ({today.strftime('%d.%m.%Y')}) или напишите дату, например <code>01.09.2026</code>."
    )


def _joined_kb(prefix: str, today: date, back: str):
    b = InlineKeyboardBuilder()
    b.row(btn("Сегодня", f"{prefix}:{today.isoformat()}"))
    b.row(*nav_row(back, back_text="✖️ Отмена"))
    return b.as_markup()


def _choice_kb(prefix: str, options: list[str], *, skip_data: str, skip_label: str, back: str):
    b = InlineKeyboardBuilder()
    row: list = []
    for index, option in enumerate(options):
        button = btn(option, f"{prefix}:{index}")
        if len(option) > 16:
            if row:
                b.row(*row)
                row = []
            b.row(button)
            continue
        row.append(button)
        if len(row) == 3:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(btn(skip_label, skip_data))
    b.row(*nav_row(back, back_text="✖️ Отмена"))
    return b.as_markup()


async def _direction_options(repo: Repo) -> list[str]:
    seen = {item.casefold() for item in DIRECTIONS}
    extra: list[str] = []
    for student in await repo.list_students(active_only=False):
        value = student.direction
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            extra.append(value)
    extra.sort(key=str.casefold)
    return [*DIRECTIONS, *extra]


async def _track_options(repo: Repo) -> list[str]:
    seen = {item.casefold() for item in TRACKS}
    extra: list[str] = []
    for student in await repo.list_students(active_only=False):
        value = student.track
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            extra.append(value)
    extra.sort(key=str.casefold)
    return [*TRACKS, *extra]


async def _group_options(repo: Repo) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for student in await repo.list_students(active_only=False):
        value = student.judaica_group
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            values.append(value)
    values.sort(key=str.casefold)
    return values


def _picked(options: list[str], token: str) -> str | None:
    index = int(token)
    if index < 0 or index >= len(options):
        raise IndexError(token)
    return options[index]


def _typed_profile(text: str, *, limit: int, known: list[str]) -> str | None:
    raw = text.strip()
    if raw.casefold() in {"пропустить", "-", "—"}:
        return None
    return match_known(clean_profile_text(raw, limit=limit), known)


def _profile_error(exc: ValueError, *, kind: str, limit: int) -> str:
    if str(exc) == "long":
        return f"{kind} должно быть короче {limit} символов."
    return f"Напишите {kind.lower()} или нажмите кнопку."


async def _ask_direction(target: CallbackQuery | Message, state: FSMContext, repo: Repo) -> None:
    options = await _direction_options(repo)
    await state.update_data(direction_options=options)
    await state.set_state(StudentSG.add_direction)
    name = (await state.get_data()).get("new_name") or ""
    await respond(
        target,
        f"Имя: <b>{esc(name)}</b>\n\nНаправление? Выберите кнопку или напишите своё. Можно пропустить.",
        _choice_kb("sad", options, skip_data="sad:skip", skip_label="Пропустить", back=SEC_STUDENTS),
    )


async def _ask_track(target: CallbackQuery | Message, state: FSMContext, repo: Repo) -> None:
    options = await _track_options(repo)
    await state.update_data(track_options=options)
    await state.set_state(StudentSG.add_track)
    await respond(
        target,
        "Молитва или тфилин клуб? Это колонка «Направление» рядом с годом поступления в журнале. Можно пропустить.",
        _choice_kb("sat", options, skip_data="sat:skip", skip_label="Пропустить", back=SEC_STUDENTS),
    )


async def _ask_group(target: CallbackQuery | Message, state: FSMContext, repo: Repo) -> None:
    options = await _group_options(repo)
    await state.update_data(group_options=options)
    await state.set_state(StudentSG.add_group)
    await respond(
        target,
        "Группа иудаики? Напишите, например <i>рав Исроэль Фельдшеров</i>, или выберите уже сохранённую. Можно пропустить.",
        _choice_kb("sag", options, skip_data="sag:skip", skip_label="Пропустить", back=SEC_STUDENTS),
    )


async def _ask_joined(target: CallbackQuery | Message, state: FSMContext, config: Config) -> None:
    await state.set_state(StudentSG.add_joined)
    today = local_today(config.timezone)
    await respond(target, _joined_prompt(today), _joined_kb("stuj", today, SEC_STUDENTS))


@router.message(StudentSG.add_year, F.text)
async def add_student_year(message: Message, state: FSMContext, repo: Repo) -> None:
    try:
        year = parse_enrollment_year(message.text or "")
    except ValueError:
        await message.answer("Год должен быть четырёхзначным, например <code>2023</code>.", reply_markup=cancel_kb(SEC_STUDENTS))
        return
    await state.update_data(new_year=year)
    await _ask_direction(message, state, repo)


@router.callback_query(StudentSG.add_direction, F.data.startswith("sad:"))
async def add_direction_btn(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    token = cb.data.split(":", 1)[1]
    if token == "skip":
        value = None
    else:
        options = list((await state.get_data()).get("direction_options") or [])
        try:
            value = _picked(options, token)
        except (IndexError, ValueError):
            await answer_callback(cb, "Список устарел, выберите ещё раз", show_alert=True)
            return
    await state.update_data(new_direction=value)
    await _ask_track(cb, state, repo)


@router.message(StudentSG.add_direction, F.text)
async def add_direction_text(message: Message, state: FSMContext, repo: Repo) -> None:
    options = list((await state.get_data()).get("direction_options") or [])
    try:
        value = _typed_profile(message.text or "", limit=DIRECTION_LIMIT, known=options)
    except ValueError as exc:
        await message.answer(_profile_error(exc, kind="Направление", limit=DIRECTION_LIMIT), reply_markup=cancel_kb(SEC_STUDENTS))
        return
    await state.update_data(new_direction=value)
    await _ask_track(message, state, repo)


@router.callback_query(StudentSG.add_track, F.data.startswith("sat:"))
async def add_track_btn(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    token = cb.data.split(":", 1)[1]
    if token == "skip":
        value = None
    else:
        options = list((await state.get_data()).get("track_options") or [])
        try:
            value = _picked(options, token)
        except (IndexError, ValueError):
            await answer_callback(cb, "Список устарел, выберите ещё раз", show_alert=True)
            return
    await state.update_data(new_track=value)
    await _ask_group(cb, state, repo)


@router.message(StudentSG.add_track, F.text)
async def add_track_text(message: Message, state: FSMContext, repo: Repo) -> None:
    options = list((await state.get_data()).get("track_options") or [])
    try:
        value = _typed_profile(message.text or "", limit=TRACK_LIMIT, known=options)
    except ValueError as exc:
        await message.answer(_profile_error(exc, kind="Значение", limit=TRACK_LIMIT), reply_markup=cancel_kb(SEC_STUDENTS))
        return
    await state.update_data(new_track=value)
    await _ask_group(message, state, repo)


@router.callback_query(StudentSG.add_group, F.data.startswith("sag:"))
async def add_group_btn(cb: CallbackQuery, state: FSMContext, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    token = cb.data.split(":", 1)[1]
    if token == "skip":
        value = None
    else:
        options = list((await state.get_data()).get("group_options") or [])
        try:
            value = _picked(options, token)
        except (IndexError, ValueError):
            await answer_callback(cb, "Список устарел, выберите ещё раз", show_alert=True)
            return
    await state.update_data(new_group=value)
    await _ask_joined(cb, state, config)


@router.message(StudentSG.add_group, F.text)
async def add_group_text(message: Message, state: FSMContext, config: Config) -> None:
    options = list((await state.get_data()).get("group_options") or [])
    try:
        value = _typed_profile(message.text or "", limit=GROUP_LIMIT, known=options)
    except ValueError as exc:
        await message.answer(_profile_error(exc, kind="Название группы", limit=GROUP_LIMIT), reply_markup=cancel_kb(SEC_STUDENTS))
        return
    await state.update_data(new_group=value)
    await _ask_joined(message, state, config)


async def _finish_add(target: CallbackQuery | Message, state: FSMContext, repo: Repo, config: Config, actor: Actor, joined: date) -> None:
    if not await require_level(target, actor, LEVEL_CHIEF_MARKER):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    student = await repo.create_student(
        data["new_name"],
        data["new_year"],
        joined.isoformat(),
        actor.telegram_id,
        direction=data.get("new_direction"),
        judaica_group=data.get("new_group"),
        track=data.get("new_track"),
    )
    await audit(
        repo,
        actor,
        "student.add",
        entity_type="student",
        entity_id=student.id,
        студент=student.full_name,
        год=data["new_year"],
        направление=data.get("new_direction"),
        молитва_тфилин=data.get("new_track"),
        группа_иудаики=data.get("new_group"),
        учитывать_с=joined.strftime("%d.%m.%Y"),
    )
    await show_student_card(target, repo, config, student.id, notice="Студент добавлен")


@router.callback_query(StudentSG.add_joined, F.data.startswith("stuj:"))
async def add_student_joined_btn(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    await _finish_add(cb, state, repo, config, actor, date.fromisoformat(cb.data.split(":", 1)[1]))


@router.message(StudentSG.add_joined, F.text)
async def add_student_joined_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        await message.answer("Не понял дату. Пример: <code>01.09.2026</code>.")
        return
    await _finish_add(message, state, repo, config, actor, day)


# ---------------------------------------------------------------- import


@router.callback_query(F.data == "stui")
async def import_start(cb: CallbackQuery, state: FSMContext, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await state.set_state(StudentSG.import_text)
    await respond(
        cb,
        "Пришлите список: каждая строка — один студент, через запятую год поступления. "
        "Год можно писать как 2023 или как 23. "
        "Дальше можно указать направление, затем «молитва» или «тфилин клуб», и группу иудаики.\n\n"
        "<code>Иван Иванов, 2023, ПИН, молитва, рав Исроэль Фельдшеров\nПётр Петров, 2024</code>\n\n"
        f"До {MAX_IMPORT_LINES} строк за раз. Перед добавлением покажу, что получилось.",
        cancel_kb(SEC_STUDENTS),
    )


async def _render_import_preview(target: CallbackQuery | Message, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    rows = data.get("import_rows") or []
    joined = date.fromisoformat(data.get("import_from") or local_today(config.timezone).isoformat())
    lines = list(data.get("import_lines") or [])
    lines.append("")
    lines.append(f"Будет добавлено: <b>{len(rows)}</b> {students_word(len(rows))}.")
    if rows:
        lines.append(f"Занятия учитываются с <b>{joined.strftime('%d.%m.%Y')}</b> (занятия раньше не войдут в процент).")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    if rows:
        b = InlineKeyboardBuilder()
        b.row(btn(f"✅ Добавить {len(rows)}", "stic"))
        b.row(btn("📅 Другая дата учёта", "stid"))
        b.row(btn("✖️ Отмена", SEC_STUDENTS))
        markup = b.as_markup()
    else:
        text += "\n\nМожно прислать исправленный список ещё раз."
        markup = cancel_kb(SEC_STUDENTS)
    await state.set_state(StudentSG.import_text)
    await respond(target, text, markup)


@router.message(StudentSG.import_text, F.text)
async def import_preview(message: Message, state: FSMContext, repo: Repo, config: Config) -> None:
    parsed = parse_student_lines(message.text or "")
    if not parsed:
        await message.answer("Не нашёл ни одной строки со студентом.", reply_markup=cancel_kb(SEC_STUDENTS))
        return
    all_students = await repo.list_students(active_only=False)
    existing = {s.full_name.casefold() for s in all_students if s.is_active}
    removed = {s.full_name.casefold() for s in all_students if not s.is_active}
    seen: set[str] = set()
    ok: list[dict] = []
    lines = ["📥 <b>Проверьте список</b>", ""]
    for item in parsed:
        if item.error:
            lines.append(f"⚠️ стр. {item.line_no}: «{esc(item.raw)}» — {esc(item.error)}")
            continue
        key = item.full_name.casefold()
        if key in existing:
            lines.append(f"↩️ {esc(item.full_name)} — уже есть, пропущу")
            continue
        if key in removed:
            lines.append(f"↩️ {esc(item.full_name)} — есть среди удалённых, пропущу. Восстановите карточку, если это тот же студент")
            continue
        if key in seen:
            lines.append(f"↩️ {esc(item.full_name)} — повтор в списке")
            continue
        seen.add(key)
        ok.append(
            {
                "name": item.full_name,
                "year": item.enrollment_year,
                "direction": item.direction,
                "group": item.judaica_group,
                "track": item.track,
            }
        )
        extra = " · ".join(part for part in (item.direction, item.track, item.judaica_group) if part)
        suffix = f" · {esc(extra)}" if extra else ""
        lines.append(f"✅ {esc(item.full_name)} — {item.enrollment_year}{suffix}")
    await state.update_data(import_rows=ok, import_lines=lines, import_from=local_today(config.timezone).isoformat())
    await _render_import_preview(message, state, config)


@router.callback_query(StudentSG.import_text, F.data == "stid")
async def import_pick_date(cb: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.set_state(StudentSG.import_date)
    await respond(cb, _joined_prompt(local_today(config.timezone)), cancel_kb(SEC_STUDENTS))


@router.message(StudentSG.import_date, F.text)
async def import_date(message: Message, state: FSMContext, config: Config) -> None:
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        await message.answer("Не понял дату. Пример: <code>01.09.2026</code>.")
        return
    await state.update_data(import_from=day.isoformat())
    await _render_import_preview(message, state, config)


@router.callback_query(StudentSG.import_text, F.data == "stic")
async def import_confirm(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    data = await state.get_data()
    rows = data.get("import_rows") or []
    joined = data.get("import_from") or local_today(config.timezone).isoformat()
    created_students = await repo.import_students(
        [(row["name"], row["year"], row.get("direction"), row.get("group"), row.get("track")) for row in rows],
        joined,
        actor.telegram_id,
    )
    await state.clear()
    created = [student.full_name for student in created_students]
    await audit(
        repo,
        actor,
        "student.import",
        количество=len(created),
        учитывать_с=date.fromisoformat(joined).strftime("%d.%m.%Y"),
        студенты=", ".join(created)[:900] or None,
    )
    await answer_callback(cb, f"Добавлено: {len(created)}")
    await show_students_home(cb, repo)


# ------------------------------------------------------------------ edit


@router.callback_query(F.data.startswith("ste:"))
async def edit_student_start(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    _, field, raw_id = cb.data.split(":", 2)
    student = await repo.get_student(int(raw_id))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    if not actor.outranks(student, config.superadmin_id):
        await answer_callback(cb, PROTECTED_STUDENT, show_alert=True)
        return
    await state.update_data(edit_student_id=student.id)
    back = f"stc:{student.id}"
    if field == "name":
        await state.set_state(StudentSG.edit_name)
        await respond(cb, f"Текущее имя: <b>{esc(student.full_name)}</b>\n\nНовое ФИО?", cancel_kb(back))
    elif field == "year":
        await state.set_state(StudentSG.edit_year)
        await respond(cb, f"Текущий год поступления: <b>{student.enrollment_year or '—'}</b>\n\nНовый год?", cancel_kb(back))
    elif field == "direction":
        options = await _direction_options(repo)
        await state.update_data(direction_options=options)
        await state.set_state(StudentSG.edit_direction)
        await respond(
            cb,
            f"Сейчас: <b>{esc(student.direction) if student.direction else '—'}</b>\n\nНовое направление? Можно написать своё.",
            _choice_kb(
                "sed",
                options,
                skip_data="sed:clear",
                skip_label="Убрать",
                back=back,
            ),
        )
    elif field == "track":
        options = await _track_options(repo)
        await state.update_data(track_options=options)
        await state.set_state(StudentSG.edit_track)
        await respond(
            cb,
            f"Сейчас: <b>{esc(student.track) if student.track else '—'}</b>\n\n"
            "Молитва или тфилин клуб? Можно написать своё.",
            _choice_kb("set", options, skip_data="set:clear", skip_label="Убрать", back=back),
        )
    elif field == "group":
        options = await _group_options(repo)
        await state.update_data(group_options=options)
        await state.set_state(StudentSG.edit_group)
        await respond(
            cb,
            f"Сейчас: <b>{esc(student.judaica_group) if student.judaica_group else '—'}</b>\n\n"
            "Новая группа иудаики? Например <i>рав Исроэль Фельдшеров</i>.",
            _choice_kb("seg", options, skip_data="seg:clear", skip_label="Убрать", back=back),
        )
    elif field == "joined":
        await state.set_state(StudentSG.edit_joined)
        await respond(
            cb,
            f"Сейчас занятия учитываются с <b>{student.joined_date.strftime('%d.%m.%Y')}</b>.\n\n"
            "С какой даты учитывать? Например <code>01.09.2026</code>. Занятия до этой даты не войдут в статистику студента.",
            cancel_kb(back),
        )


async def _apply_student_edit(
    target: CallbackQuery | Message,
    state: FSMContext,
    repo: Repo,
    config: Config,
    actor: Actor,
    field: str,
    value,
    label: str,
) -> None:
    if not await require_level(target, actor, LEVEL_CHIEF_MARKER):
        await state.clear()
        return
    data = await state.get_data()
    student = await repo.get_student(int(data.get("edit_student_id") or 0))
    await state.clear()
    if student is None:
        await respond(target, "Студент не найден.")
        return
    if not actor.outranks(student, config.superadmin_id):
        await respond(target, PROTECTED_STUDENT)
        return
    before = getattr(student, field)
    await repo.update_student(student.id, **{field: value})
    await audit(repo, actor, "student.update", entity_type="student", entity_id=student.id, студент=student.full_name, поле=label, было=before, стало=value)
    await show_student_card(target, repo, config, student.id)


@router.message(StudentSG.edit_name, F.text)
async def edit_student_name(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    name = normalize_name(message.text or "")
    if len(name.split()) < 2:
        await message.answer("Нужны минимум фамилия и имя.")
        return
    data = await state.get_data()
    student_id = int(data.get("edit_student_id") or 0)
    other = await repo.find_student_by_name(name, active_only=False)
    if other is not None and other.id != student_id:
        back = cancel_kb(f"stc:{student_id}") if student_id else cancel_kb(SEC_STUDENTS)
        if other.is_active:
            await message.answer(f"Студент <b>{esc(name)}</b> уже есть в группе.", reply_markup=back)
        else:
            await message.answer(
                f"<b>{esc(name)}</b> есть среди удалённых. Восстановите ту карточку, если это тот же студент.",
                reply_markup=back,
            )
        return
    await _apply_student_edit(message, state, repo, config, actor, "full_name", name, "имя")


@router.message(StudentSG.edit_year, F.text)
async def edit_student_year(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    try:
        year = parse_enrollment_year(message.text or "")
    except ValueError:
        await message.answer("Год должен быть четырёхзначным, например <code>2023</code>.")
        return
    await _apply_student_edit(message, state, repo, config, actor, "enrollment_year", year, "год поступления")


@router.callback_query(StudentSG.edit_direction, F.data.startswith("sed:"))
async def edit_direction_btn(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    token = cb.data.split(":", 1)[1]
    if token == "clear":
        value = None
    else:
        options = list((await state.get_data()).get("direction_options") or [])
        try:
            value = _picked(options, token)
        except (IndexError, ValueError):
            await answer_callback(cb, "Список устарел, выберите ещё раз", show_alert=True)
            return
    await _apply_student_edit(cb, state, repo, config, actor, "direction", value, "направление")


@router.message(StudentSG.edit_direction, F.text)
async def edit_direction_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    options = list((await state.get_data()).get("direction_options") or [])
    try:
        value = _typed_profile(message.text or "", limit=DIRECTION_LIMIT, known=options)
    except ValueError as exc:
        await message.answer(_profile_error(exc, kind="Направление", limit=DIRECTION_LIMIT))
        return
    await _apply_student_edit(message, state, repo, config, actor, "direction", value, "направление")


@router.callback_query(StudentSG.edit_track, F.data.startswith("set:"))
async def edit_track_btn(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    token = cb.data.split(":", 1)[1]
    if token == "clear":
        value = None
    else:
        options = list((await state.get_data()).get("track_options") or [])
        try:
            value = _picked(options, token)
        except (IndexError, ValueError):
            await answer_callback(cb, "Список устарел, выберите ещё раз", show_alert=True)
            return
    await _apply_student_edit(cb, state, repo, config, actor, "track", value, "молитва / тфилин")


@router.message(StudentSG.edit_track, F.text)
async def edit_track_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    options = list((await state.get_data()).get("track_options") or [])
    try:
        value = _typed_profile(message.text or "", limit=TRACK_LIMIT, known=options)
    except ValueError as exc:
        await message.answer(_profile_error(exc, kind="Значение", limit=TRACK_LIMIT))
        return
    await _apply_student_edit(message, state, repo, config, actor, "track", value, "молитва / тфилин")


@router.callback_query(StudentSG.edit_group, F.data.startswith("seg:"))
async def edit_group_btn(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    token = cb.data.split(":", 1)[1]
    if token == "clear":
        value = None
    else:
        options = list((await state.get_data()).get("group_options") or [])
        try:
            value = _picked(options, token)
        except (IndexError, ValueError):
            await answer_callback(cb, "Список устарел, выберите ещё раз", show_alert=True)
            return
    await _apply_student_edit(cb, state, repo, config, actor, "judaica_group", value, "группа иудаики")


@router.message(StudentSG.edit_group, F.text)
async def edit_group_text(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    options = list((await state.get_data()).get("group_options") or [])
    try:
        value = _typed_profile(message.text or "", limit=GROUP_LIMIT, known=options)
    except ValueError as exc:
        await message.answer(_profile_error(exc, kind="Название группы", limit=GROUP_LIMIT))
        return
    await _apply_student_edit(message, state, repo, config, actor, "judaica_group", value, "группа иудаики")


@router.message(StudentSG.edit_joined, F.text)
async def edit_student_joined(message: Message, state: FSMContext, repo: Repo, config: Config, actor: Actor) -> None:
    try:
        day = parse_date_text(message.text or "", today=local_today(config.timezone))
    except ValueError:
        await message.answer("Не понял дату. Пример: <code>01.09.2026</code>.")
        return
    await _apply_student_edit(message, state, repo, config, actor, "joined_on", day.isoformat(), "учитывать с")


# --------------------------------------------------------- remove/restore


@router.callback_query(F.data.startswith("std:"))
async def remove_student_ask(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    student = await repo.get_student(int(cb.data.split(":", 1)[1]))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    await respond(
        cb,
        f"Удалить <b>{esc(student.full_name)}</b> из группы?\n\n"
        "История посещений сохранится: в отчётах за месяцы, когда студент числился, он останется. "
        "Из списка отметок пропадёт, занятия после сегодняшнего дня ему считаться не будут. Роль будет снята.",
        confirm_kb(f"stdc:{student.id}", f"stc:{student.id}", yes_text="🗑 Удалить"),
    )


@router.callback_query(F.data.startswith("stdc:"))
async def remove_student(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    student = await repo.get_student(int(cb.data.split(":", 1)[1]))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    if actor.student is not None and actor.student.id == student.id:
        await answer_callback(cb, "Нельзя удалить самого себя.", show_alert=True)
        return
    if not actor.outranks(student, config.superadmin_id):
        await answer_callback(cb, PROTECTED_STUDENT, show_alert=True)
        return
    await repo.remove_student(student.id, local_today(config.timezone).isoformat())
    await audit(repo, actor, "student.remove", entity_type="student", entity_id=student.id, студент=student.full_name, роль_была=role_title(student.role) if student.role else None)
    await answer_callback(cb, "Студент удалён")
    await show_students_home(cb, repo)


async def show_removed(cb: CallbackQuery, repo: Repo, page: int) -> None:
    students = [s for s in await repo.list_students(active_only=False) if not s.is_active]
    page, pages, size = fit_page(len(students), page, reserved=len(nav_row(SEC_STUDENTS)))
    b = InlineKeyboardBuilder()
    for student in students[page * size : (page + 1) * size]:
        left = student.left_date.strftime("%d.%m.%Y") if student.left_date else ""
        b.row(btn(f"{student.full_name[:32]} · {left}", f"stc:{student.id}"))
    if pages > 1:
        b.row(*pager_row("sturp", page, pages))
    b.row(*nav_row(SEC_STUDENTS))
    text = "🗂 <b>Удалённые студенты</b>" + ("\n\nНикого нет." if not students else "")
    await respond(cb, text, b.as_markup())


@router.callback_query(F.data == "stur")
async def removed_students(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await state.clear()
    await show_removed(cb, repo, 0)


@router.callback_query(F.data.startswith("sturp:"))
async def removed_students_page(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await show_removed(cb, repo, int(cb.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("stre:"))
async def restore_student(cb: CallbackQuery, state: FSMContext, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    await state.clear()
    assert actor is not None
    student = await repo.get_student(int(cb.data.split(":", 1)[1]))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    await repo.restore_student(student.id)
    await audit(repo, actor, "student.restore", entity_type="student", entity_id=student.id, студент=student.full_name)
    await show_student_card(cb, repo, config, student.id, notice="Восстановлен")


@router.callback_query(F.data.startswith("stunl:"))
async def unlink_ask(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    student = await repo.get_student(int(cb.data.split(":", 1)[1]))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    await respond(
        cb,
        f"Отвязать Telegram от <b>{esc(student.full_name)}</b>? Роль будет снята, потребуется новая верификация.",
        confirm_kb(f"stunlc:{student.id}", f"stc:{student.id}", yes_text="🔓 Отвязать"),
    )


@router.callback_query(F.data.startswith("stunlc:"))
async def unlink(cb: CallbackQuery, repo: Repo, config: Config, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_CHIEF_MARKER):
        return
    assert actor is not None
    student = await repo.get_student(int(cb.data.split(":", 1)[1]))
    if student is None:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    if actor.student is not None and actor.student.id == student.id:
        await answer_callback(cb, "Нельзя отвязать самого себя.", show_alert=True)
        return
    if not actor.outranks(student, config.superadmin_id):
        await answer_callback(cb, PROTECTED_STUDENT, show_alert=True)
        return
    await repo.unbind_telegram(student.id)
    await audit(repo, actor, "student.unlink", entity_type="student", entity_id=student.id, студент=student.full_name, telegram_id=student.telegram_id)
    await show_student_card(cb, repo, config, student.id, notice="Telegram отвязан")


# ------------------------------------------------------------ verification


@router.callback_query(F.data == SEC_VERIFY)
async def open_verify(cb: CallbackQuery, state: FSMContext, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_MARKER):
        return
    await show_student_picker(
        cb,
        state,
        repo,
        ctx=VERIFY_CTX,
        title="🔗 <b>Верификация</b>\nВыберите студента, которому нужна ссылка (показаны только ещё не подтверждённые):",
        back=NAV_MAIN,
        only=PICK_FILTER_ONLY_UNVERIFIED,
    )


@router.callback_query(F.data.startswith(f"pk:{VERIFY_CTX}:"))
async def verify_pick(cb: CallbackQuery, state: FSMContext, repo: Repo, bot: Bot, actor: Actor | None) -> None:
    await state.clear()
    await issue_link(cb, repo, bot, actor, int(cb.data.rsplit(":", 1)[1]))


@router.callback_query(F.data.startswith("verl:"))
async def make_link(cb: CallbackQuery, repo: Repo, bot: Bot, actor: Actor | None) -> None:
    await issue_link(cb, repo, bot, actor, int(cb.data.split(":", 1)[1]))


async def issue_link(cb: CallbackQuery, repo: Repo, bot: Bot, actor: Actor | None, student_id: int) -> None:
    if not await require_level(cb, actor, LEVEL_MARKER):
        return
    assert actor is not None
    student = await repo.get_student(student_id)
    if student is None or not student.is_active:
        await answer_callback(cb, "Студент не найден", show_alert=True)
        return
    if student.is_verified:
        await answer_callback(cb, "Студент уже подтверждён.", show_alert=True)
        return
    link = await repo.create_verification_link(student.id, actor.telegram_id)
    me = await bot.me()
    url = f"https://t.me/{me.username}?start=v_{link.token}"
    await audit(repo, actor, "verification.link", entity_type="student", entity_id=student.id, студент=student.full_name)
    text = (
        f"🔗 Ссылка верификации для <b>{esc(student.full_name)}</b>:\n\n"
        f"<code>{url}</code>\n\n"
        "Перешлите её студенту. Ссылка одноразовая; предыдущие ссылки для этого студента отозваны."
    )
    b = InlineKeyboardBuilder()
    b.row(btn("🔗 Ещё студент", SEC_VERIFY))
    b.row(*nav_row())
    await respond(cb, text, b.as_markup())
