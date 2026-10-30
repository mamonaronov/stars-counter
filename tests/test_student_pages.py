"""Student keyboards stay on one screen until they hit Telegram's button limit."""

from database.models import Event, Student
from keyboards.main import MAX_INLINE_BUTTONS, marking_kb, marking_page_for, students_picker_kb


def _student(i: int, *, joined: str = "2026-09-01") -> Student:
    return Student(
        id=i,
        full_name=f"Студент {i:03d}",
        enrollment_year=2023,
        telegram_id=None,
        verified_at=None,
        role=None,
        joined_on=joined,
        left_on=None,
        status="active",
        created_by=1,
        created_at="",
        updated_at="",
    )


def _event() -> Event:
    return Event(
        id=7,
        title="Урок",
        kind="mandatory",
        event_date="2026-10-01",
        start_time=None,
        template_id=None,
        closes_month=0,
        month_title=None,
        created_by=1,
        created_at="",
        updated_at="",
        deleted_at=None,
    )


def _buttons(markup) -> list[tuple[str, str]]:
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def test_picker_shows_the_whole_group_without_a_pager():
    students = [_student(i) for i in range(1, 21)]
    buttons = _buttons(students_picker_kb(students, "stu", 0, back="s:stu"))
    names = [text for text, _ in buttons if text.startswith("Студент")]
    assert names == [student.full_name for student in students]
    assert not any(text == "1/2" for text, _ in buttons)
    assert len(buttons) == len(students) + 2


def test_picker_pages_only_at_the_button_limit():
    # Back and menu take two buttons, so 98 students still fit on one keyboard.
    fits = [_student(i) for i in range(98)]
    one_screen = _buttons(students_picker_kb(fits, "stu", 0, back="s:stu"))
    assert len(one_screen) == MAX_INLINE_BUTTONS
    assert not any("/" in text for text, _ in one_screen)

    students = [_student(i) for i in range(99)]
    first = _buttons(students_picker_kb(students, "stu", 0, back="s:stu"))
    assert len(first) == MAX_INLINE_BUTTONS
    assert any(text == "1/2" for text, _ in first)
    assert "Студент 094" in {text for text, _ in first}
    assert "Студент 098" not in {text for text, _ in first}

    second = _buttons(students_picker_kb(students, "stu", 1, back="s:stu"))
    assert any(text == "2/2" for text, _ in second)
    assert "Студент 098" in {text for text, _ in second}
    assert "Студент 000" not in {text for text, _ in second}


def test_marking_pages_the_same_way_and_keeps_the_student_page():
    event = _event()
    few = [_student(i) for i in range(1, 6)]
    buttons = _buttons(marking_kb(event, few, set(), back="ev:7"))
    assert [text for text, _ in buttons if text.startswith("Студент")] == [s.full_name for s in few]
    assert ("✔️ Готово", "ev:7") in buttons
    assert not any(data.startswith("mkp:") for _, data in buttons)

    students = [_student(i) for i in range(99)]
    first = _buttons(marking_kb(event, students, {0}, back="ev:7"))
    assert len(first) == MAX_INLINE_BUTTONS
    assert any(data == "mkp:7:1" for _, data in first)
    assert marking_page_for(event, students, 0) == 0
    assert marking_page_for(event, students, 98) == 1
    second = _buttons(marking_kb(event, students, set(), back="ev:7", page=1))
    assert any(text.startswith("Студент 098") for text, _ in second)
