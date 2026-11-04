"""Broadcast audiences and the marker send flow."""

from __future__ import annotations

import pytest

from database.database import Database
from database.models import User
from services.broadcast import (
    AudienceCatalog,
    BroadcastPerson,
    audience_catalog,
    broadcast_people,
    broadcast_recipients,
    selected_labels,
    toggle_group,
)
from tests.conftest import make_config, student
from tests.harness import Harness

SUPER = 1
MARKER = 11
STUDENT = 12
OTHER = 13
REMOVED = 14
GUEST = 99


def _person(telegram_id: int, card=None, *, superadmin: bool = False) -> BroadcastPerson:
    return BroadcastPerson(telegram_id, card, superadmin)


def test_groups_are_a_union_and_all_replaces_them():
    pin = student(2, "Пин Пин", telegram_id=2, direction="ПИН", judaica_group="Алеф", track="молитва")
    marker = student(3, "Эк Эк", telegram_id=3, direction="ЭК", role="marker")
    chief = student(8, "Главный", telegram_id=8, role="chief_marker")
    removed = student(4, "Удалён", telegram_id=4, direction="ПИН", status="removed")
    unverified = student(5, "Без телеграма", direction="ПИН")
    catalog = audience_catalog([pin, marker, chief, removed, unverified])
    assert catalog.directions == ("ПИН", "ЭК")
    assert catalog.groups == ("Алеф",)
    assert catalog.tracks == ("молитва",)
    assert catalog.years == (2024,)

    people = [
        _person(1),
        _person(2, pin),
        _person(3, marker),
        _person(4, removed),
        _person(8, chief),
        _person(9, superadmin=True),
    ]
    assert broadcast_recipients(people, {"g", "d0"}, catalog) == [1, 2]
    assert broadcast_recipients(people, {"m"}, catalog) == [3]
    assert broadcast_recipients(people, {"cm"}, catalog) == [8]
    assert broadcast_recipients(people, {"s"}, catalog) == [2]
    assert broadcast_recipients(people, {"sa"}, catalog) == [9]
    assert broadcast_recipients(people, {"y0"}, catalog) == [2, 3, 8]
    assert broadcast_recipients(people, {"j0"}, catalog) == [2]
    assert broadcast_recipients(people, {"t0"}, catalog) == [2]
    assert broadcast_recipients(people, {"all"}, catalog, exclude=2) == [1, 3, 4, 8, 9]
    assert broadcast_recipients(people, set(), catalog) == []
    assert broadcast_recipients(people, {"d9"}, catalog) == []
    assert selected_labels({"g", "d0"}, catalog) == ["Гости", "Направление: ПИН"]

    assert toggle_group({"g", "d0"}, "all") == {"all"}
    assert toggle_group({"all"}, "g") == {"g"}
    assert toggle_group({"g", "d0"}, "g") == {"d0"}


def test_people_keep_guests_removed_cards_and_the_superadmin():
    users = [User(1, None, "Гость", None, "t", "t")]
    linked = student(2, "Живой", telegram_id=2)
    removed = student(3, "Удалён", telegram_id=3, status="removed")
    inactive_dup = student(4, "Старая", telegram_id=2, status="removed")
    people = broadcast_people(users, [linked, removed, inactive_dup, student(5, "Нет сети")], superadmin_id=1)
    by_id = {person.telegram_id: person for person in people}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1].is_superadmin and by_id[1].student is None
    assert by_id[2].student is not None and by_id[2].student.full_name == "Живой"
    assert by_id[3].student is not None and by_id[3].student.status == "removed"


@pytest.fixture
async def h(tmp_path):
    config = make_config(tmp_path)
    db = Database(config)
    await db.initialize()
    try:
        yield Harness(config, db)
    finally:
        await db.close()


async def test_marker_sends_to_the_chosen_groups_only(h: Harness):
    pin = await h.repo.create_student("Пин Студент", 2023, "2026-09-01", SUPER, direction="ПИН")
    other = await h.repo.create_student("Эк Студент", 2023, "2026-09-01", SUPER, direction="ЭК")
    marker = await h.repo.create_student("Отмечающий Один", 2023, "2026-09-01", SUPER)
    removed = await h.repo.create_student("Бывший Пин", 2023, "2026-09-01", SUPER, direction="ПИН")
    await h.repo.bind_telegram(pin.id, STUDENT)
    await h.repo.bind_telegram(other.id, OTHER)
    await h.repo.bind_telegram(marker.id, MARKER)
    await h.repo.bind_telegram(removed.id, REMOVED)
    await h.repo.set_role(marker.id, "marker")
    await h.repo.remove_student(removed.id, "2026-10-01")

    await h.text(GUEST, "/start")
    await h.text(STUDENT, "/start")
    await h.text(OTHER, "/start")
    await h.text(REMOVED, "/start")
    await h.text(MARKER, "/start")
    assert "📢 Сообщение" in h.buttons()

    await h.text(STUDENT, "/start")
    assert "📢 Сообщение" not in h.buttons()
    await h.click(STUDENT, "s:bc")
    assert h.session.last("AnswerCallbackQuery").text == "Недостаточно прав для этого действия."

    await h.text(MARKER, "/start")
    await h.click(MARKER, "s:bc")
    screen = h.last_text()
    assert "Кто в группах" in screen
    assert "не привязаны к карточке" in screen
    assert "без роли" in screen
    assert "только эта роль" in screen
    assert "Направление" in screen and "в том числе если у них есть роль" in screen
    await h.press(MARKER, "Дальше")
    assert "Сначала выберите" in h.session.last("AnswerCallbackQuery").text

    await h.press(MARKER, "Гости")
    await h.press(MARKER, "Направление")
    assert "этого направления" in h.last_text()
    await h.press(MARKER, "ПИН")
    await h.press(MARKER, "Дальше")
    assert "Гости" in h.last_text() and "ПИН" in h.last_text()
    await h.text(MARKER, "Завтра <b>занятие</b>")
    assert "Отправить 2 людям" in h.last_text()
    own_before = [text for text in h.session.messages_to(MARKER) if "Завтра" in text]
    await h.press(MARKER, "Отправить")
    assert "Доставлено: <b>2</b>" in h.last_text()

    def delivered(chat_id: int) -> list[str]:
        return [text for text in h.session.messages_to(chat_id) if "Завтра" in text]

    assert delivered(GUEST) and delivered(STUDENT)
    assert "&lt;b&gt;занятие&lt;/b&gt;" in delivered(STUDENT)[-1]
    assert "Отмечающий Один" in delivered(STUDENT)[-1]
    assert delivered(OTHER) == []
    assert delivered(REMOVED) == []
    assert delivered(MARKER) == own_before
    rows = await h.repo.list_audit()
    assert any(row.action == "broadcast.send" for row in rows)
