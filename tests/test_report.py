from __future__ import annotations

import re
import zipfile
from datetime import date
from io import BytesIO

from openpyxl import load_workbook

from services.report import PeriodSection, build_periods_xlsx, build_report_xlsx
from services.stats import compute_group_stats
from tests.conftest import event, excuse, student


def _book(data: bytes):
    return load_workbook(BytesIO(data))


def test_report_journal_layout():
    students = [
        student(1, "Антонов Антон", direction="ПИН", judaica_group="рав Исроэль Фельдшеров", track="молитва"),
        student(2, "Борисов Борис"),
    ]
    events = [event(1, "2026-09-01"), event(2, "2026-09-03", kind="free", title="Доп"), event(3, "2026-09-05")]
    stats = compute_group_stats(students, events, {1: {1, 2}, 2: {1}}, [excuse(2, "2026-09-05", "2026-09-05")])
    wb = _book(
        build_report_xlsx(
            org_title="Посещаемость групп",
            period_title="Сентябрь 2026",
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 5),
            stats=stats,
            events=events,
        )
    )
    ws = wb["Сентябрь 2026"]
    assert ws["H1"].value == "День недели"
    assert ws["H2"].value == "Число месяца"
    assert ws["A3"].value == "Курс"
    assert ws["B3"].value == "ФИО"
    assert ws["C3"].value == "% посещения"
    assert ws["D3"].value == "Кол-во посещения"
    assert ws["F3"].value == "Год поступления"
    assert ws["G3"].value == "Направление"
    assert ws["H3"].value == "Группа иудаики"
    assert (ws["I1"].value, ws["J1"].value, ws["K1"].value) == ("вторник", "четверг", "суббота")
    assert (ws["I2"].value, ws["J2"].value, ws["K2"].value) == (1, 3, 5)
    assert (ws["I3"].value, ws["J3"].value, ws["K3"].value) == ("Лекция", "Доп", "Лекция")
    assert (ws["I4"].value, ws["J4"].value, ws["K4"].value) == (1, None, 1)
    assert ws["L3"].value is None
    assert ws["L4"].value == "=SUM(I4:K4)"
    assert ws["J3"].fill.fgColor.rgb.endswith("FCE5CD")

    assert ws["B5"].value == "Антонов Антон"
    assert ws["A5"].value == 3
    assert ws["C5"].number_format == "0%"
    assert ws["E5"].value == "ПИН"
    assert ws["F5"].value == 24
    assert ws["G5"].value == "молитва"
    assert ws["H5"].value == "рав Исроэль Фельдшеров"
    assert ws["E6"].value is None
    assert ws["H6"].value is None
    assert ws["C5"].value == "=D5/$L$4"
    assert ws["D5"].value == "=SUM(I5:K5)"
    assert (ws["I5"].value, ws["J5"].value, ws["K5"].value) == (1, 1, None)

    assert ws["B6"].value == "Борисов Борис"
    assert ws["C6"].value == "=D6/$L$4"
    assert (ws["I6"].value, ws["J6"].value, ws["K6"].value) == (1, None, 1)
    assert ws["K6"].comment is None
    assert ws["K6"].fill.fgColor.rgb.endswith("C9DAF8")
    assert ws["B7"].value == "Освобождение по заявлению"
    assert ws["A7"].fill.fgColor.rgb.endswith("D9EAD3")
    assert ws["B8"].value == "Освобождение по мед причине"
    assert ws["A8"].fill.fgColor.rgb.endswith("C9DAF8")
    assert ws["B9"].value == "Свободный день"
    assert ws["A9"].fill.fgColor.rgb.endswith("FCE5CD")
    assert ws["B10"].value == "Вне срока ученика"
    assert ws["A10"].fill.fgColor.rgb.endswith("C9EDED")
    assert ws["B11"].value is None
    assert ws["A10"].border.bottom.style == "thick"
    assert ws["C10"].border.right.style == "thick"
    assert ws["H7"].value == "% посещения занятия"
    assert ws["H8"].value == "человек пришло"
    assert ws["I8"].value == "=SUM(I5:I6)"
    assert ws["I7"].value == "=I8/2"
    assert ws["I7"].number_format == "0.00%"
    assert ws["C5"].number_format == "0%"
    assert ws.auto_filter.ref == "A4:K8"
    assert ws.freeze_panes is None


def _sheet(students, events, attendance, *, interim=False, period_from=date(2026, 9, 1), period_to=date(2026, 9, 7)):
    return _book(
        build_report_xlsx(
            org_title="Посещаемость групп",
            period_title="Сентябрь 2026",
            period_from=period_from,
            period_to=period_to,
            stats=compute_group_stats(students, events, attendance, []),
            events=events,
            interim=interim,
        )
    )["Сентябрь 2026"]


def test_column_widths_follow_the_text_in_the_cells():
    short = _sheet([student(1, "Иванов Иван")], [event(1, "2026-09-01")], {1: {1}})
    long = _sheet(
        [student(1, "Холодненко Дмитрий Александрович")],
        [event(1, "2026-09-01")],
        {1: {1}},
    )
    assert long.column_dimensions["B"].width > short.column_dimensions["B"].width

    empty = _sheet([student(1, "Иванов Иван")], [event(1, "2026-09-01")], {})
    full = _sheet([student(1, "Иванов Иван")], [event(1, "2026-09-01")], {1: {1}})
    assert full.column_dimensions["C"].width > empty.column_dimensions["C"].width
    assert full.column_dimensions["C"].width < 12

    interim = _sheet([student(1, "Иванов Иван")], [event(1, "2026-09-01")], {1: {1}}, interim=True)
    assert interim["A1"].value == "промежуточный"
    assert interim.column_dimensions["A"].width < 8


def test_same_day_lessons_share_the_weekday_and_fit_their_percent():
    students = [student(1, "Антонов Антон")]
    pair = _sheet(
        students,
        [event(1, "2026-09-01", title="Утро"), event(2, "2026-09-01", title="Вечер")],
        {1: {1}, 2: {1}},
        period_to=date(2026, 9, 1),
    )
    monday = _sheet(students, [event(1, "2026-09-07")], {1: {1}})
    assert pair.column_dimensions["I"].width == pair.column_dimensions["J"].width
    assert pair.column_dimensions["I"].width >= 8
    assert pair.column_dimensions["I"].width < monday.column_dimensions["I"].width
    assert pair["I6"].number_format == "0.00%"
    assert pair["J6"].number_format == "0.00%"


def _cached_formula(data: bytes, ref: str, *, sheet: str = "xl/worksheets/sheet1.xml") -> tuple[str, str]:
    xml = zipfile.ZipFile(BytesIO(data)).read(sheet).decode()
    match = re.search(rf'<c r="{ref}"[^>]*>(.*?)</c>', xml)
    assert match, ref
    body = match.group(1)
    formula = re.search(r"<f[^>]*>(.*?)</f>", body)
    value = re.search(r"<v>(.*?)</v>", body)
    assert formula and value, body
    return formula.group(1), value.group(1)


def test_formula_results_are_cached_for_iphone():
    students = [student(1, "Антонов Антон"), student(2, "Борисов Борис")]
    events = [event(1, "2026-09-01"), event(2, "2026-09-03", kind="free"), event(3, "2026-09-05")]
    stats = compute_group_stats(students, events, {1: {1, 2}, 2: {1}, 3: {1}}, [])
    data = build_report_xlsx(
        org_title="Посещаемость групп",
        period_title="Сентябрь 2026",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 5),
        stats=stats,
        events=events,
    )
    # Antonov attended both mandatory lessons and the free one: 3 / 2 = 150%.
    assert _cached_formula(data, "D5") == ("SUM(I5:K5)", "3")
    assert _cached_formula(data, "C5") == ("D5/$L$4", "1.5")
    # Borisov attended only the first mandatory lesson: 1 / 2 = 50%.
    assert _cached_formula(data, "D6") == ("SUM(I6:K6)", "1")
    assert _cached_formula(data, "C6") == ("D6/$L$4", "0.5")
    assert _cached_formula(data, "L4") == ("SUM(I4:K4)", "2")
    assert _cached_formula(data, "I8") == ("SUM(I5:I6)", "2")
    assert _cached_formula(data, "I7") == ("I8/2", "1")
    assert _cached_formula(data, "J7") == ("J8/2", "0.5")
    book = zipfile.ZipFile(BytesIO(data)).read("xl/workbook.xml").decode()
    assert 'fullCalcOnLoad="1"' not in book
    assert 'calcCompleted="1"' in book


def test_percent_styles_are_marked_so_iphone_excel_applies_them():
    students = [student(1, "Антонов Антон"), student(2, "Борисов Борис")]
    events = [event(1, "2026-09-01"), event(2, "2026-09-05")]
    stats = compute_group_stats(students, events, {1: {1}}, [])
    data = build_report_xlsx(
        org_title="Посещаемость групп",
        period_title="Сентябрь 2026",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 5),
        stats=stats,
        events=events,
    )
    styles = zipfile.ZipFile(BytesIO(data)).read("xl/styles.xml").decode()
    openings = re.findall(r"<xf\b[^>]*>", styles)
    percent = [tag for tag in openings if re.search(r'numFmtId="(?:9|10)"', tag)]
    general = [tag for tag in openings if 'numFmtId="0"' in tag]
    assert percent
    assert all('applyNumberFormat="1"' in tag for tag in percent)
    assert general
    assert all("applyNumberFormat=" not in tag for tag in general)


def test_report_undefined_percent_is_not_zero():
    students = [student(1, "Антонов Антон")]
    events = [event(1, "2026-09-03", kind="free")]
    stats = compute_group_stats(students, events, {1: {1}}, [])
    assert stats[0].percent is None
    wb = _book(
        build_report_xlsx(
            org_title="Посещаемость групп",
            period_title="Сентябрь 2026",
            period_from=date(2026, 9, 3),
            period_to=date(2026, 9, 3),
            stats=stats,
            events=events,
        )
    )
    ws = wb["Сентябрь 2026"]
    assert ws["C5"].value == "—"
    assert ws["I4"].value is None
    assert ws["I5"].value == 1
    assert ws["J4"].value == "=SUM(I4:I4)"


def test_report_percent_formulas_and_interim_mark():
    students = [student(1, "Антонов Антон"), student(2, "Борисов Борис")]
    events = [event(1, "2026-09-01"), event(2, "2026-09-03", kind="free"), event(3, "2026-09-05")]
    stats = compute_group_stats(students, events, {1: {1, 2}, 2: {1}, 3: {1}}, [])
    by_name = {item.student.full_name: item.percent for item in stats}
    assert by_name["Антонов Антон"] == 150.0
    assert by_name["Борисов Борис"] == 50.0
    wb = _book(
        build_report_xlsx(
            org_title="Посещаемость групп",
            period_title="Сентябрь 2026",
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 5),
            stats=stats,
            events=events,
            interim=True,
        )
    )
    ws = wb["Сентябрь 2026"]
    assert ws["A1"].value == "промежуточный"
    assert (ws["I5"].value, ws["J5"].value, ws["K5"].value) == (1, 1, 1)
    assert (ws["I6"].value, ws["J6"].value, ws["K6"].value) == (1, None, None)
    assert len(ws.conditional_formatting._cf_rules) == 1
    rules = next(iter(ws.conditional_formatting._cf_rules.values()))
    assert [rule.formula[0] for rule in rules] == ["0.85", "0.7", "0.6", "0.5", "0"]
    assert all(rule.stopIfTrue for rule in rules)
    assert rules[0].dxf.fill.bgColor.rgb.endswith("B6D7A8")
    assert rules[-1].dxf.fill.bgColor.rgb.endswith("EA9999")


def test_same_day_lessons_share_a_header():
    students = [student(1, "Антонов Антон")]
    events = [
        event(1, "2026-09-01", title="Шахарит", time="08:00"),
        event(2, "2026-09-01", title="Иудаика", time="10:00"),
    ]
    stats = compute_group_stats(students, events, {1: {1}, 2: {1}}, [])
    wb = _book(
        build_report_xlsx(
            org_title="Посещаемость групп",
            period_title="Сентябрь 2026",
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 1),
            stats=stats,
            events=events,
        )
    )
    ws = wb["Сентябрь 2026"]
    assert {str(item) for item in ws.merged_cells.ranges} >= {"I1:J1", "I2:J2"}
    assert ws["I1"].value == "вторник"
    assert ws["I2"].value == 1
    assert (ws["I3"].value, ws["J3"].value) == ("Шахарит", "Иудаика")
    assert (ws["I5"].value, ws["J5"].value) == (1, 1)


def test_late_joiner_uses_own_denominator():
    students = [student(1, "Антонов Антон"), student(2, "Борисов Борис", joined="2026-09-05")]
    events = [event(1, "2026-09-01"), event(2, "2026-09-05")]
    stats = compute_group_stats(students, events, {2: {2}}, [])
    wb = _book(
        build_report_xlsx(
            org_title="Посещаемость групп",
            period_title="Сентябрь 2026",
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 5),
            stats=stats,
            events=events,
        )
    )
    ws = wb["Сентябрь 2026"]
    assert ws["C5"].value == "=D5/$K$4"
    assert (ws["I5"].value, ws["J5"].value) == (None, None)
    assert ws["C6"].value == "=D6/SUM($J$4:$J$4)"
    assert ws["D6"].value == "=SUM(J6:J6)"
    assert (ws["I6"].value, ws["J6"].value) == (None, 1)
    assert ws["I6"].fill.fgColor.rgb.endswith("C9EDED")


def test_free_lesson_inside_a_mandatory_day_stays_plain():
    students = [student(1, "Антонов Антон")]
    events = [
        event(1, "2026-09-01", title="Шахарит", time="08:00"),
        event(2, "2026-09-01", title="Хасидут", time="10:00", kind="free"),
    ]
    stats = compute_group_stats(students, events, {1: {1}, 2: {1}}, [])
    wb = _book(
        build_report_xlsx(
            org_title="Посещаемость групп",
            period_title="Сентябрь 2026",
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 1),
            stats=stats,
            events=events,
        )
    )
    ws = wb["Сентябрь 2026"]
    assert (ws["I4"].value, ws["J4"].value) == (1, None)
    assert ws["J5"].value == 1
    assert ws["J3"].fill.fgColor is None or ws["J3"].fill.fgColor.theme != 9
    assert ws["I1"].fill.patternType in (None, "none")


def test_archive_has_one_sheet_per_period():
    students = [student(1, "Антонов Антон")]
    closed_events = [event(1, "2026-09-01", closes=True)]
    open_events = [event(2, "2026-10-02", title="Семинар")]
    closed = compute_group_stats(students, closed_events, {1: {1}}, [])
    opened = compute_group_stats(students, open_events, {}, [])
    wb = _book(
        build_periods_xlsx(
            org_title="Посещаемость групп",
            sections=[
                PeriodSection("Сентябрь: 2026", date(2026, 9, 1), date(2026, 9, 1), closed, closed_events),
                PeriodSection(
                    "Текущий: Октябрь 2026",
                    date(2026, 10, 2),
                    date(2026, 10, 2),
                    opened,
                    open_events,
                    interim=True,
                ),
            ],
        )
    )
    assert len(wb.sheetnames) == 2
    assert all(re.search(r"[\\*?:/\[\]]", name) is None for name in wb.sheetnames)
    assert wb.sheetnames == ["Сентябрь 2026", "Текущий Октябрь 2026"]
    assert "промежуточный" in wb[wb.sheetnames[1]]["A1"].value
    assert wb[wb.sheetnames[0]].cell(row=5, column=9).value == 1
    assert wb[wb.sheetnames[1]].cell(row=3, column=9).value == "Семинар"
