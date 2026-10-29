from __future__ import annotations

import pytest

from services.students import normalize_name, parse_enrollment_year, parse_student_lines


def test_normalize_name():
    assert normalize_name("  иван   иванов ") == "Иван Иванов"
    assert normalize_name("ПЕТРОВ-ВОДКИН кузьма") == "Петров-Водкин Кузьма"


def test_parse_comma_format():
    rows = parse_student_lines("иван иванов, 2023\nПётр Петров;2024\n\n")
    assert [(r.full_name, r.enrollment_year, r.error) for r in rows] == [
        ("Иван Иванов", 2023, None),
        ("Пётр Петров", 2024, None),
    ]


def test_parse_year_without_separator():
    rows = parse_student_lines("Сидоров Сидор Сидорович 2022")
    assert rows[0].full_name == "Сидоров Сидор Сидорович"
    assert rows[0].enrollment_year == 2022


def test_parse_short_enrollment_year():
    rows = parse_student_lines(
        "иван иванов, 23\n"
        "Пётр Петров;24, ЭК\n"
        "Сидоров Сидор Сидорович 22\n"
        "Андрей Андреев, 25, ЮР, тфилин клуб, рав Давид Шапиро\n"
        "Борис Борисов, 023\n"
    )
    assert [(row.full_name, row.enrollment_year, row.direction, row.track, row.judaica_group, row.error) for row in rows] == [
        ("Иван Иванов", 2023, None, None, None, None),
        ("Пётр Петров", 2024, "ЭК", None, None, None),
        ("Сидоров Сидор Сидорович", 2022, None, None, None, None),
        ("Андрей Андреев", 2025, "ЮР", "тфилин клуб", "рав Давид Шапиро", None),
        ("Борис Борисов", None, None, None, None, "не понял год: «023»"),
    ]


def test_parse_errors():
    rows = parse_student_lines("Иван\nИван Иванов\nИван Иванов, вчера")
    assert rows[0].error == "нужны минимум фамилия и имя"
    assert rows[1].error == "не указан год поступления"
    assert rows[2].error and "не понял год" in rows[2].error


def test_parse_direction_and_judaica_group():
    rows = parse_student_lines(
        "Иван Иванов, 2023, ПИН, рав Исроэль Фельдшеров\n"
        "Пётр Петров, 2024, ЭК\n"
        "Сидор Сидоров, 2022,, рав Давид Шапиро\n"
        "Андрей Андреев, 2025, ЮР, тфилин клуб, рав Давид Шапиро\n"
    )
    assert [(row.direction, row.track, row.judaica_group, row.error) for row in rows] == [
        ("ПИН", None, "рав Исроэль Фельдшеров", None),
        ("ЭК", None, None, None),
        (None, None, "рав Давид Шапиро", None),
        ("ЮР", "тфилин клуб", "рав Давид Шапиро", None),
    ]


def test_parse_enrollment_year():
    assert parse_enrollment_year(" 2023 ") == 2023
    with pytest.raises(ValueError):
        parse_enrollment_year("23")
