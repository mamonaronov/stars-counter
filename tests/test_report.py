from __future__ import annotations

from datetime import date
from io import BytesIO

from openpyxl import load_workbook

from services.report import build_report_xlsx
from services.stats import compute_group_stats
from tests.conftest import event, excuse, student


def test_report_has_ranking_and_details():
    students = [student(1, "Антонов Антон"), student(2, "Борисов Борис")]
    events = [event(1, "2026-09-01"), event(2, "2026-09-03", kind="free", title="Доп"), event(3, "2026-09-05")]
    stats = compute_group_stats(students, events, {1: {1, 2}, 2: {1}}, [excuse(2, "2026-09-05", "2026-09-05")])
    data = build_report_xlsx(
        org_title="Посещаемость групп",
        period_title="Сентябрь 2026",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 5),
        stats=stats,
        events=events,
    )
    wb = load_workbook(BytesIO(data))
    ws = wb["Отчёт"]
    assert ws["A1"].value == "Посещаемость групп за период"
    assert ws["A2"].value == "За период с 01.09.2026 по 05.09.2026"
    assert (ws["A3"].value, ws["B3"].value, ws["C3"].value) == ("№", "ФИО", "Процент")
    assert ws["B4"].value == "Антонов Антон"
    # Антонов: 1 of 2 mandatory + 1 free = 100%; Борисов: 1 of 2 + 1 excused = 100%
    assert ws["C4"].value == 1.0
    assert ws["B5"].value == "Борисов Борис"
    assert ws["C5"].value == 1.0


def test_report_undefined_percent_is_not_zero():
    students = [student(1, "Антонов Антон")]
    events = [event(1, "2026-09-03", kind="free")]
    stats = compute_group_stats(students, events, {1: {1}}, [])
    assert stats[0].percent is None
    data = build_report_xlsx(
        org_title="Посещаемость групп",
        period_title="Сентябрь 2026",
        period_from=date(2026, 9, 3),
        period_to=date(2026, 9, 3),
        stats=stats,
        events=events,
    )
    wb = load_workbook(BytesIO(data))
    assert wb["Отчёт"]["C4"].value == "—"


def test_report_percent_values():
    students = [student(1, "Антонов Антон"), student(2, "Борисов Борис")]
    events = [event(1, "2026-09-01"), event(2, "2026-09-03", kind="free"), event(3, "2026-09-05")]
    stats = compute_group_stats(students, events, {1: {1, 2}, 2: {1}, 3: {1}}, [])
    by_name = {s.student.full_name: s.percent for s in stats}
    assert by_name["Антонов Антон"] == 150.0
    assert by_name["Борисов Борис"] == 50.0
    data = build_report_xlsx(
        org_title="Посещаемость групп",
        period_title="Сентябрь 2026",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 5),
        stats=stats,
        events=events,
        interim=True,
    )
    wb = load_workbook(BytesIO(data))
    ws = wb["Отчёт"]
    assert "промежуточный" in ws["A1"].value
    assert ws["C4"].value == 1.5
    assert ws["C5"].value == 0.5
    details = wb["Детали"]
    assert details.cell(row=5, column=2).value == "Антонов Антон"
    assert details.cell(row=5, column=8).value == "+"
