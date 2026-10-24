"""End-to-end flows through the real handlers with a fake Telegram session."""

from __future__ import annotations

import re

import pytest

from database.database import Database
from tests.conftest import make_config
from tests.harness import Harness

SUPER = 1
CHIEF = 10
MARKER = 11
STUDENT = 12
GUEST = 99


@pytest.fixture
async def h(tmp_path):
    config = make_config(tmp_path)
    db = Database(config)
    await db.initialize()
    try:
        yield Harness(config, db)
    finally:
        await db.close()


async def verify(h: Harness, student_name_part: str, user_id: int) -> None:
    """Superadmin issues a link, the user follows it."""
    await h.click(SUPER, "s:ver")
    await h.press(SUPER, student_name_part)
    match = re.search(r"start=v_([\w-]+)", h.last_text())
    assert match, h.last_text()
    await h.text(user_id, f"/start v_{match.group(1)}")
    assert "Вы подтверждены" in h.session.messages_to(user_id)[-2]


async def test_guest_sees_public_menu_only(h: Harness):
    await h.text(GUEST, "/start")
    buttons = h.buttons()
    assert "📊 Посещаемость студентов" in buttons
    assert "➕ Новое занятие" not in buttons
    assert "👤 Моя посещаемость" not in buttons
    # permission check is enforced in the handler, not just by hiding the button
    await h.click(GUEST, "s:new")
    assert h.session.last("AnswerCallbackQuery").text == "Недостаточно прав для этого действия."


async def test_full_flow(h: Harness):
    # superadmin menu
    await h.text(SUPER, "/start")
    assert "🛠 Сервис" in h.buttons()

    # bulk import with preview
    await h.click(SUPER, "s:stu")
    await h.press(SUPER, "Импорт списком")
    await h.text(SUPER, "иванов иван, 2023\nпетров пётр; 2024\nСидоров Сидор Сидорович 2022\nбез года\nиванов иван, 2023")
    text = h.last_text()
    assert "✅ Иванов Иван — 2023" in text
    assert "↩️ Иванов Иван — повтор в списке" in text
    assert "⚠️" in text and "без года" in text
    assert "Будет добавлено: <b>3</b>" in text
    # events are back-dated to September, so count students from 01.09
    await h.press(SUPER, "Другая дата учёта")
    await h.text(SUPER, "01.09.2026")
    assert "учитываются с <b>01.09.2026</b>" in h.last_text()
    await h.press(SUPER, "Добавить 3")
    students = await h.repo.list_students()
    assert [s.full_name for s in students] == ["Иванов Иван", "Петров Пётр", "Сидоров Сидор Сидорович"]
    assert {s.joined_on for s in students} == {"2026-09-01"}

    # add one more by hand
    await h.click(SUPER, "s:stu")
    await h.press(SUPER, "➕ Добавить")
    await h.text(SUPER, "козлов кирилл")
    await h.text(SUPER, "2023")
    await h.press(SUPER, "ПИН")
    await h.text(SUPER, "рав Давид Шапиро")
    await h.press(SUPER, "Сегодня")
    assert "Козлов Кирилл" in h.last_text()
    kozlov = next(item for item in await h.repo.list_students() if item.full_name == "Козлов Кирилл")
    assert kozlov.direction == "ПИН"
    assert kozlov.judaica_group == "рав Давид Шапиро"

    # verification of three users
    await verify(h, "Иванов", CHIEF)
    await verify(h, "Петров", MARKER)
    await verify(h, "Сидоров", STUDENT)
    ivanov = await h.repo.get_student_by_telegram(CHIEF)
    assert ivanov is not None and ivanov.full_name == "Иванов Иван"

    # a used link cannot be reused
    link = (await h.repo._fetchall("SELECT token FROM verification_links WHERE used_at IS NOT NULL"))[0][0]
    await h.text(GUEST, f"/start v_{link}")
    assert "уже использована" in h.session.messages_to(GUEST)[-2]

    # superadmin makes Ivanov chief marker; chief marker makes Petrov marker
    await h.click(SUPER, "s:rol")
    await h.press(SUPER, "Назначить")
    await h.press(SUPER, "Иванов")
    await h.press(SUPER, "главный отмечающий")
    assert (await h.repo.get_student_by_telegram(CHIEF)).role == "chief_marker"
    assert any("главный отмечающий" in m for m in h.session.messages_to(CHIEF))

    await h.text(CHIEF, "/menu")
    assert "👥 Студенты" in h.buttons() and "🛠 Сервис" not in h.buttons()
    await h.click(CHIEF, "s:rol")
    await h.press(CHIEF, "Назначить")
    await h.press(CHIEF, "Петров")
    labels = list(h.buttons())
    assert any("→ отмечающий" in l for l in labels) and not any("→ админ" in l for l in labels)
    await h.press(CHIEF, "→ отмечающий")
    assert (await h.repo.get_student_by_telegram(MARKER)).role == "marker"

    # marker cannot change the chief's role
    await h.text(MARKER, "/menu")
    assert "🎖 Роли" not in h.buttons()
    await h.click(MARKER, "rola:1:marker")
    assert "Недостаточно прав" in h.session.last("AnswerCallbackQuery").text

    # marker creates a template and an event from it, dated in the past
    await h.click(MARKER, "s:tpl")
    await h.press(MARKER, "Новый шаблон")
    await h.text(MARKER, "Лекция по истории")
    await h.press(MARKER, "Обязательное")
    await h.text(MARKER, "9:00")
    assert any("Лекция по истории" in b and "09:00" in b for b in h.buttons())

    await h.click(MARKER, "s:new")
    await h.press(MARKER, "Лекция по истории")
    await h.text(MARKER, "01.09.2026")
    await h.press(MARKER, "⏰ 09:00")
    assert "Отметка присутствия" in h.last_text()
    events = await h.repo.list_events()
    assert len(events) == 1 and events[0].event_date == "2026-09-01" and events[0].start_time == "09:00"
    event1 = events[0]

    # toggle attendance on and off through the buttons
    await h.press(MARKER, "Иванов")
    assert await h.repo.is_present(event1.id, ivanov.id)
    assert "✅ Иванов Иван" in h.buttons()
    assert "Отмечено: <b>1</b>" in h.last_text()
    await h.press(MARKER, "✅ Иванов Иван")
    assert not await h.repo.is_present(event1.id, ivanov.id)
    await h.press(MARKER, "Иванов")
    await h.press(MARKER, "Петров")
    await h.press(MARKER, "Сидоров")
    assert "Отмечено: <b>3</b>" in h.last_text()

    # manual event (no template), free kind, "now"
    await h.click(MARKER, "s:new")
    await h.press(MARKER, "Без шаблона")
    await h.text(MARKER, "05.09.2026")
    await h.press(MARKER, "Без времени")
    await h.text(MARKER, "Субботник")
    await h.press(MARKER, "Свободное")
    event2 = (await h.repo.list_events())[-1]
    assert event2.kind == "free" and event2.title == "Субботник"
    await h.press(MARKER, "Иванов")

    # third mandatory event, nobody but Ivanov
    await h.click(MARKER, "s:new")
    await h.press(MARKER, "Лекция по истории")
    await h.text(MARKER, "10.09.2026")
    await h.press(MARKER, "Без времени")
    event3 = (await h.repo.list_events())[-1]
    await h.press(MARKER, "Иванов")

    # sick leave for Petrov covering event3
    await h.click(MARKER, "s:exc")
    await h.press(MARKER, "➕ Добавить")
    await h.press(MARKER, "Петров")
    await h.press(MARKER, "Больничный")
    await h.text(MARKER, "09.09.2026")
    await h.text(MARKER, "12.09.2026")
    await h.text(MARKER, "справка 12")
    assert "Больничный" in h.last_text() and "09.09.2026 — 12.09.2026" in h.last_text()

    # public stats: guest looks at Petrov -> 2 mandatory, 1 present, 1 excused = 100%
    await h.text(GUEST, "/start")
    await h.click(GUEST, "s:att")
    await h.press(GUEST, "Петров")
    await h.press(GUEST, "Текущий")
    text = h.last_text()
    assert "<b>100,00%</b>" in text and "🩺" in text
    # Ivanov: 2/2 + 1 free = 150%
    await h.click(GUEST, "s:att")
    await h.press(GUEST, "Иванов")
    await h.press(GUEST, "Текущий")
    assert "<b>150,00%</b>" in h.last_text()

    # student request about event3, marker approves it
    await h.text(STUDENT, "/menu")
    assert "👤 Моя посещаемость" in h.buttons()
    await h.click(STUDENT, "s:nreq")
    await h.press(STUDENT, "Текущий")
    await h.press(STUDENT, "10.09")
    await h.text(STUDENT, "Я был, сидел сзади")
    assert any("Новый запрос" in m for m in h.session.messages_to(MARKER))
    await h.text(MARKER, "/menu")
    assert "📨 Запросы (1)" in h.buttons()
    await h.click(MARKER, "s:req")
    await h.press(MARKER, "Сидоров")
    await h.press(MARKER, "✅ Отметить")
    assert await h.repo.is_present(event3.id, (await h.repo.get_student_by_telegram(STUDENT)).id)
    assert any("запрос одобрен" in m for m in h.session.messages_to(STUDENT))
    assert await h.repo.count_open_requests() == 0

    # close the month at event3, then report
    await h.click(MARKER, "s:mon")
    await h.press(MARKER, "Текущий")
    await h.press(MARKER, "Закрыть месяц")
    await h.press(MARKER, "10.09")
    assert "Сентябрь 2026" in h.last_text() and "закрыт" in h.last_text()
    await h.press(MARKER, "Отчёт Excel")
    doc = h.session.last("SendDocument")
    assert doc is not None and doc.document.filename.endswith(".xlsx")
    assert "Отчёт: Сентябрь 2026" in doc.caption

    # the next event opens a new period automatically
    await h.click(MARKER, "s:new")
    await h.press(MARKER, "Лекция по истории")
    await h.text(MARKER, "02.10.2026")
    await h.press(MARKER, "Без времени")
    await h.click(MARKER, "s:mon")
    labels = list(h.buttons())
    assert any("Сентябрь 2026 · 3 зан." in l for l in labels)
    assert any("Текущий: Октябрь 2026 · 1 зан." in l for l in labels)

    # move the month end to event2, rename, reopen
    await h.press(MARKER, "Сентябрь 2026")
    await h.press(MARKER, "Другое последнее занятие")
    await h.press(MARKER, "05.09")
    assert (await h.repo.get_event(event2.id)).closes_month == 1
    assert (await h.repo.get_event(event3.id)).closes_month == 0
    await h.press(MARKER, "Переименовать")
    await h.text(MARKER, "Сентябрь (укороченный)")
    assert "Сентябрь (укороченный)" in h.last_text()
    await h.press(MARKER, "Открыть обратно")
    await h.press(MARKER, "❌ Открыть обратно")
    assert all(e.closes_month == 0 for e in await h.repo.list_events())

    # audit log is visible to markers and records roles
    await h.click(MARKER, "s:aud")
    text = h.last_text()
    assert "Журнал действий" in text and "(отмечающий) открыл месяц обратно" in text
    await h.press(MARKER, "»")
    await h.press(MARKER, "»")
    assert "(суперадмин) добавил студента" in h.last_text()
    # per-event history
    await h.click(MARKER, f"evh:{event1.id}:0")
    assert "отметил присутствие" in h.last_text() and "снял отметку" in h.last_text()
    await h.click(STUDENT, "s:aud")
    assert "Недостаточно прав" in h.session.last("AnswerCallbackQuery").text

    # chief transfers his role to Petrov, keeps "marker"
    await h.text(CHIEF, "/menu")
    await h.click(CHIEF, "s:rol")
    await h.press(CHIEF, "Передать роль")
    await h.press(CHIEF, "Петров")
    await h.press(CHIEF, "🔁 Передать")
    assert (await h.repo.get_student_by_telegram(MARKER)).role == "chief_marker"
    assert (await h.repo.get_student_by_telegram(CHIEF)).role == "marker"

    # removing a student hides them from marking; history stays
    await h.text(MARKER, "/menu")
    await h.click(MARKER, "s:stu")
    await h.press(MARKER, "📋 Список")
    await h.press(MARKER, "Козлов")
    await h.press(MARKER, "Удалить из группы")
    await h.press(MARKER, "🗑 Удалить")
    assert len(await h.repo.list_students()) == 3
    await h.click(MARKER, f"mark:{event1.id}")
    assert "Козлов" not in " ".join(h.buttons())

    # superadmin service panel + manual backup
    await h.click(SUPER, "s:srv")
    assert "Версия БД" in h.last_text()
    await h.press(SUPER, "Прислать бэкап")
    name = h.session.last("SendDocument").document.filename
    assert name.startswith("stars-counter-backup_") and name.endswith(".tgz")


async def test_lower_role_cannot_remove_or_unlink_higher(h: Harness):
    chief = await h.repo.create_student("Главный Отмечающий", 2023, "2026-09-01", SUPER)
    admin = await h.repo.create_student("Админ Админович", 2023, "2026-09-01", SUPER)
    await h.repo.bind_telegram(chief.id, CHIEF)
    await h.repo.bind_telegram(admin.id, 50)
    await h.repo.set_role(chief.id, "chief_marker")
    await h.repo.set_role(admin.id, "admin")
    await h.text(CHIEF, "/start")
    await h.click(CHIEF, f"std:{admin.id}")
    assert "Удалить" in h.last_text()
    await h.press(CHIEF, "🗑 Удалить")
    assert "Нельзя" in h.session.last("AnswerCallbackQuery").text
    assert (await h.repo.get_student(admin.id)).is_active
    assert (await h.repo.get_student(admin.id)).role == "admin"
    await h.click(CHIEF, f"stunlc:{admin.id}")
    assert "Нельзя" in h.session.last("AnswerCallbackQuery").text
    assert (await h.repo.get_student(admin.id)).telegram_id == 50


async def test_rename_refuses_an_existing_name(h: Harness):
    await h.repo.create_student("Иванов Иван", 2023, "2026-09-01", SUPER)
    other = await h.repo.create_student("Петров Пётр", 2023, "2026-09-01", SUPER)
    removed = await h.repo.create_student("Сидоров Сидор", 2023, "2026-09-01", SUPER)
    await h.repo.remove_student(removed.id, "2026-10-01")
    await h.text(SUPER, "/start")
    await h.click(SUPER, f"ste:name:{other.id}")
    await h.text(SUPER, "иванов иван")
    assert "уже есть" in h.last_text()
    assert (await h.repo.get_student(other.id)).full_name == "Петров Пётр"
    await h.text(SUPER, "сидоров сидор")
    assert "удалённых" in h.last_text()
    assert (await h.repo.get_student(other.id)).full_name == "Петров Пётр"


async def test_marking_skips_student_not_yet_enrolled(h: Harness):
    late = await h.repo.create_student("Поздний Пётр", 2024, "2026-10-01", SUPER)
    event = await h.repo.create_event("Лекция", "mandatory", "2026-09-01", "10:00", None, SUPER)
    await h.text(SUPER, "/start")
    await h.click(SUPER, f"mark:{event.id}")
    assert "Не учитываются на эту дату: 1" in h.last_text()
    await h.press(SUPER, "Поздний")
    assert not await h.repo.is_present(event.id, late.id)
    assert "не учитывается" in h.session.last("AnswerCallbackQuery").text


async def test_card_closer_moves_existing_boundary(h: Harness):
    first = await h.repo.create_event("Первое", "mandatory", "2026-09-01", None, None, SUPER)
    second = await h.repo.create_event("Второе", "mandatory", "2026-09-10", None, None, SUPER)
    await h.repo.update_event(second.id, closes_month=1, month_title="Сентябрь 2026")
    await h.text(SUPER, "/start")
    await h.click(SUPER, f"ev:{first.id}")
    await h.press(SUPER, "🔚 Последнее в месяце")
    assert (await h.repo.get_event(first.id)).closes_month == 1
    assert (await h.repo.get_event(first.id)).month_title == "Сентябрь 2026"
    assert (await h.repo.get_event(second.id)).closes_month == 0


async def test_excuse_list_is_paged(h: Harness):
    student = await h.repo.create_student("Больной Борис", 2023, "2026-09-01", SUPER)
    for day in range(1, 10):
        await h.repo.create_excuse(student.id, "sick", f"2026-09-{day:02d}", f"2026-09-{day:02d}", None, SUPER)
    await h.text(SUPER, "/start")
    await h.click(SUPER, "s:exc")
    assert "1/2" in h.buttons()
    first_page = [text for text in h.buttons() if text.startswith("🤒")]
    await h.press(SUPER, "»")
    assert "2/2" in h.buttons()
    second_page = [text for text in h.buttons() if text.startswith("🤒")]
    assert first_page and second_page and set(first_page).isdisjoint(second_page)


async def test_deleting_month_closer_keeps_the_month(h: Harness):
    first = await h.repo.create_event("Первое", "mandatory", "2026-09-01", None, None, SUPER)
    second = await h.repo.create_event("Второе", "mandatory", "2026-09-10", None, None, SUPER)
    await h.repo.update_event(second.id, closes_month=1, month_title="Сентябрь 2026")
    later = await h.repo.create_event("Октябрь", "mandatory", "2026-10-02", None, None, SUPER)
    await h.text(SUPER, "/start")
    await h.click(SUPER, f"evd:{second.id}")
    await h.press(SUPER, "🗑 Удалить")
    assert (await h.repo.get_event(second.id)).deleted_at
    kept = await h.repo.get_event(first.id)
    assert kept.closes_month == 1 and kept.month_title == "Сентябрь 2026"
    assert (await h.repo.get_event(later.id)).closes_month == 0


async def test_time_edit_tells_when_event_changes_month(h: Harness):
    closer = await h.repo.create_event("Утро", "mandatory", "2026-09-10", "09:00", None, SUPER)
    later = await h.repo.create_event("День", "mandatory", "2026-09-10", "12:00", None, SUPER)
    await h.repo.update_event(closer.id, closes_month=1, month_title="Сентябрь 2026")
    await h.text(SUPER, "/start")
    await h.click(SUPER, f"ev:{later.id}")
    await h.press(SUPER, "🕐 Время")
    await h.text(SUPER, "08:00")
    assert "теперь в месяце «Сентябрь 2026»" in h.last_text()
    assert (await h.repo.get_event(later.id)).start_time == "08:00"


async def test_month_end_cannot_jump_over_a_period(h: Harness):
    first = await h.repo.create_event("Сентябрь", "mandatory", "2026-09-01", None, None, SUPER)
    october = await h.repo.create_event("Октябрь", "mandatory", "2026-10-01", None, None, SUPER)
    november = await h.repo.create_event("Ноябрь", "mandatory", "2026-11-01", None, None, SUPER)
    await h.repo.update_event(first.id, closes_month=1, month_title="Сентябрь 2026")
    await h.repo.update_event(october.id, closes_month=1, month_title="Октябрь 2026")
    await h.repo.update_event(november.id, closes_month=1, month_title="Ноябрь 2026")
    await h.text(SUPER, "/start")
    await h.click(SUPER, f"monce:{first.id}:{november.id}")
    assert "другого месяца" in h.session.last("AnswerCallbackQuery").text
    assert (await h.repo.get_event(first.id)).closes_month == 1
    assert (await h.repo.get_event(october.id)).month_title == "Октябрь 2026"
    assert (await h.repo.get_event(november.id)).month_title == "Ноябрь 2026"


async def test_removed_student_keeps_own_attendance(h: Harness):
    student = await h.repo.create_student("Бывший Борис", 2023, "2026-09-01", SUPER)
    await h.repo.bind_telegram(student.id, STUDENT)
    await h.repo.remove_student(student.id, "2026-10-01")
    await h.text(STUDENT, "/start")
    assert "👤 Моя посещаемость" in h.buttons()
    assert "не верифицированы" not in h.last_text()
    assert not any("Новое занятие" in b for b in h.buttons())
    await h.click(STUDENT, "s:my")
    assert "Бывший Борис" in h.last_text()
    await h.text(GUEST, "/start")
    await h.click(GUEST, "s:att")
    await h.press(GUEST, "Бывший")
    assert "Бывший Борис" in h.last_text()


async def test_same_name_does_not_create_a_second_card(h: Harness):
    student = await h.repo.create_student("Иванов Иван", 2023, "2026-09-01", SUPER)
    await h.repo.remove_student(student.id, "2026-10-01")
    await h.text(SUPER, "/start")
    await h.click(SUPER, "s:stu")
    await h.press(SUPER, "➕ Добавить")
    await h.text(SUPER, "Иванов Иван")
    assert "среди удалённых" in h.last_text()
    assert len(await h.repo.list_students(active_only=False)) == 1
    await h.press(SUPER, "Восстановить")
    restored = await h.repo.get_student(student.id)
    assert restored.is_active and restored.left_on is None


async def test_student_picker_filter(h: Harness):
    for name in ("Антонов Антон", "Борисов Борис", "Антипов Пётр"):
        await h.repo.create_student(name, 2023, "2026-09-01", SUPER)
    await h.text(GUEST, "/start")
    await h.click(GUEST, "s:att")
    assert len([b for b in h.buttons() if "pk:att:" in h.buttons()[b]]) == 3
    await h.text(GUEST, "анти")
    picks = {t: d for t, d in h.buttons().items() if d.startswith("pk:att:")}
    assert list(picks) == ["Антипов Пётр"]
    assert "найдено 1" in h.last_text()


async def test_only_chief_admin_can_wipe_and_must_type_the_word(h: Harness):
    chief = await h.repo.create_student("Главный Админ", 2023, "2026-09-01", SUPER)
    admin = await h.repo.create_student("Обычный Админ", 2023, "2026-09-01", SUPER)
    await h.repo.bind_telegram(chief.id, 40)
    await h.repo.bind_telegram(admin.id, 41)
    await h.repo.set_role(chief.id, "chief_admin")
    await h.repo.set_role(admin.id, "admin")
    await h.repo.create_event("Лекция", "mandatory", "2026-09-01", "10:00", None, SUPER)
    await h.repo.db.set_system("markers_group_id", "-100")
    backup = await h.db.backup(prefix="before_wipe")
    version = await h.db.user_version()

    await h.text(41, "/start")
    assert not any("Очистить все данные" in text for text in h.buttons())
    await h.click(41, "s:wip")
    assert h.session.last("AnswerCallbackQuery").text == "Недостаточно прав для этого действия."
    assert len(await h.repo.list_students()) == 2

    await h.text(40, "/start")
    assert any("Очистить все данные" in text for text in h.buttons())
    await h.click(40, "w:2")
    assert "первое предупреждение" in h.session.last("AnswerCallbackQuery").text
    assert "Очистить все данные" in h.last_text()
    assert backup.exists()

    await h.press(40, "Я понимаю")
    assert "безвозвратно" in h.last_text()
    await h.press(40, "Отмена")
    assert len(await h.repo.list_students()) == 2
    assert await h.db.get_system("markers_group_id") == "-100"

    await h.press(40, "Очистить все данные")
    await h.press(40, "Я понимаю")
    await h.press(40, "Всё равно")
    assert "СТЕРЕТЬ" in h.last_text()
    await h.text(40, "удалить")
    assert "не то слово" in h.last_text()
    assert len(await h.repo.list_students()) == 2

    await h.text(40, "стереть")
    assert any("Все данные удалены" in text for text in h.session.messages_to(40))
    assert any("Все данные бота стёрты" in text for text in h.session.messages_to(SUPER))
    assert await h.repo.list_students(active_only=False) == []
    assert await h.repo.list_events() == []
    assert await h.db.get_system("markers_group_id") is None
    assert not backup.exists()
    assert await h.db.user_version() == version
    assert h.config.superadmin_id == SUPER
    assert not any("Очистить все данные" in text for text in h.buttons())

    await h.text(SUPER, "/menu")
    assert any("Очистить все данные" in text for text in h.buttons())
    assert "🛠 Сервис" in h.buttons()
