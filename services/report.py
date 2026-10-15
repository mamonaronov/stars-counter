"""Excel report: ranking by attendance percent plus a detailed matrix sheet."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from io import BytesIO

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from database.models import Event
from services.stats import (
    STATUS_ABSENT,
    STATUS_EXCUSED,
    STATUS_FREE_SKIPPED,
    STATUS_NOT_COUNTED,
    STATUS_PRESENT,
    StudentStats,
)

_THIN = Side(style="thin", color="999999")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_HEADER_FILL = PatternFill("solid", fgColor="F2F2F2")

_MATRIX_MARK = {
    STATUS_PRESENT: "+",
    STATUS_ABSENT: "",
    STATUS_EXCUSED: "у",
    STATUS_FREE_SKIPPED: "",
    STATUS_NOT_COUNTED: "·",
}
_BAD_SHEET = re.compile(r"[\\*?:/\[\]]")


@dataclass(frozen=True, slots=True)
class PeriodSection:
    period_title: str
    period_from: date | None
    period_to: date | None
    stats: list[StudentStats]
    events: list[Event]
    interim: bool = False


def _percent_cell(item: StudentStats):
    """None means there were no mandatory events, so the percent is undefined — not zero."""
    if item.percent is None:
        return "—"
    return item.percent / 100


def _fmt(day: date | None) -> str:
    return day.strftime("%d.%m.%Y") if day else "—"


def _sheet_name(raw: str, used: set[str]) -> str:
    cleaned = _BAD_SHEET.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" '") or "Лист"
    cleaned = cleaned[:31]
    candidate = cleaned
    number = 2
    while candidate.casefold() in used:
        suffix = f" {number}"
        candidate = f"{cleaned[: 31 - len(suffix)]}{suffix}".strip()
        number += 1
    used.add(candidate.casefold())
    return candidate


def build_report_xlsx(
    *,
    org_title: str,
    period_title: str,
    period_from: date | None,
    period_to: date | None,
    stats: list[StudentStats],
    events: list[Event],
    interim: bool = False,
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Отчёт"
    _write_ranking(
        ws,
        org_title=org_title,
        period_from=period_from,
        period_to=period_to,
        stats=stats,
        interim=interim,
    )
    _details_sheet(wb, "Детали", period_title, stats, events)
    return _save(wb)


def build_periods_xlsx(*, org_title: str, sections: list[PeriodSection]) -> bytes:
    """One ranking sheet and one matrix sheet for every period, in chronological order."""
    if not sections:
        raise ValueError("no sections")
    wb = Workbook()
    used: set[str] = set()
    first = True
    for index, section in enumerate(sections, start=1):
        report_name = _sheet_name(f"{index}. {section.period_title}", used)
        if first:
            ws = wb.active
            ws.title = report_name
            first = False
        else:
            ws = wb.create_sheet(report_name)
        _write_ranking(
            ws,
            org_title=org_title,
            period_from=section.period_from,
            period_to=section.period_to,
            stats=section.stats,
            interim=section.interim,
        )
        _details_sheet(
            wb,
            _sheet_name(f"{index}. Детали", used),
            section.period_title,
            section.stats,
            section.events,
        )
    return _save(wb)


def _save(wb: Workbook) -> bytes:
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _write_ranking(
    ws,
    *,
    org_title: str,
    period_from: date | None,
    period_to: date | None,
    stats: list[StudentStats],
    interim: bool,
) -> None:
    heading = f"{org_title} за период" + (" (промежуточный)" if interim else "")
    ws.merge_cells("A1:C1")
    ws["A1"] = heading
    ws["A1"].font = Font(bold=True, size=12)
    ws["A1"].alignment = Alignment(horizontal="center")

    ws.merge_cells("A2:C2")
    ws["A2"] = f"За период с {_fmt(period_from)} по {_fmt(period_to)}"
    ws["A2"].alignment = Alignment(horizontal="center")

    headers = ("№", "ФИО", "Процент")
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(row=3, column=col, value=text)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.border = _BORDER
        cell.alignment = Alignment(horizontal="center" if col != 2 else "left")

    row = 4
    for index, item in enumerate(stats, start=1):
        ws.cell(row=row, column=1, value=index).border = _BORDER
        name_cell = ws.cell(row=row, column=2, value=item.student.full_name)
        name_cell.border = _BORDER
        pct_cell = ws.cell(row=row, column=3, value=_percent_cell(item))
        if item.percent is not None:
            pct_cell.number_format = "0.00%"
        pct_cell.border = _BORDER
        pct_cell.alignment = Alignment(horizontal="right")
        row += 1
    last_row = row - 1

    if stats:
        ws.conditional_formatting.add(
            f"C4:C{last_row}",
            ColorScaleRule(
                start_type="num",
                start_value=0,
                start_color="F8696B",
                mid_type="num",
                mid_value=0.6,
                mid_color="FFEB84",
                end_type="num",
                end_value=1,
                end_color="63BE7B",
            ),
        )
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["C"].width = 12
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:C{max(last_row, 3)}"


def _details_sheet(
    wb: Workbook,
    sheet_name: str,
    period_title: str,
    stats: list[StudentStats],
    events: list[Event],
) -> None:
    ws = wb.create_sheet(sheet_name)
    ws["A1"] = period_title
    ws["A1"].font = Font(bold=True, size=12)
    ws["A2"] = "+ был, у — уважительная причина, пусто — пропуск, · — не учитывается. Свободные занятия помечены (св.)"
    ws["A2"].font = Font(italic=True, color="666666")

    fixed = ("№", "ФИО", "Обязат.", "Был", "Свободн.", "Уваж.", "Процент")
    header_row = 4
    for col, text in enumerate(fixed, start=1):
        cell = ws.cell(row=header_row, column=col, value=text)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.border = _BORDER
    for offset, event in enumerate(events):
        col = len(fixed) + 1 + offset
        label = f"{event.day.strftime('%d.%m')}\n{event.title}" + ("\n(св.)" if event.is_free else "")
        cell = ws.cell(row=header_row, column=col, value=label)
        cell.font = Font(bold=True, size=9)
        cell.fill = _HEADER_FILL
        cell.border = _BORDER
        cell.alignment = Alignment(wrap_text=True, horizontal="center", vertical="top", text_rotation=90)
        ws.column_dimensions[get_column_letter(col)].width = 4
    ws.row_dimensions[header_row].height = 110

    row = header_row + 1
    for index, item in enumerate(stats, start=1):
        values = (
            index,
            item.student.full_name,
            item.mandatory_total,
            item.attended_mandatory,
            item.attended_free,
            item.excused,
            _percent_cell(item),
        )
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.border = _BORDER
            if col == len(fixed) and item.percent is not None:
                cell.number_format = "0.00%"
        by_event = {r.event.id: r for r in item.rows}
        for offset, event in enumerate(events):
            col = len(fixed) + 1 + offset
            r = by_event.get(event.id)
            mark = _MATRIX_MARK.get(r.status, "") if r else ""
            cell = ws.cell(row=row, column=col, value=mark)
            cell.border = _BORDER
            cell.alignment = Alignment(horizontal="center")
        row += 1

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 38
    for letter in ("C", "D", "E", "F"):
        ws.column_dimensions[letter].width = 9
    ws.column_dimensions["G"].width = 10
    ws.freeze_panes = ws.cell(row=header_row + 1, column=3)
