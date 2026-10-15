"""Student name normalisation and bulk import parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
MAX_IMPORT_LINES = 200


def normalize_name(raw: str) -> str:
    """'иван   иванов' -> 'Иван Иванов'; keeps hyphenated parts capitalised."""
    words = [w for w in re.split(r"\s+", raw.strip()) if w]

    def cap(word: str) -> str:
        return "-".join(part[:1].upper() + part[1:].lower() if part else part for part in word.split("-"))

    return " ".join(cap(w) for w in words)


@dataclass(slots=True)
class ParsedStudent:
    line_no: int
    raw: str
    full_name: str
    enrollment_year: int | None
    error: str | None = None


def parse_student_line(line_no: int, line: str) -> ParsedStudent | None:
    raw = line.strip().strip("•-–—").strip()
    if not raw:
        return None
    year: int | None = None
    separator = re.search(r"[,;]", raw)
    if separator:
        name_part, tail = raw[: separator.start()], raw[separator.end() :]
        match = _YEAR_RE.search(tail)
        if match:
            year = int(match.group(0))
        elif tail.strip():
            return ParsedStudent(line_no, raw, normalize_name(name_part), None, f"не понял год: «{tail.strip()}»")
    else:
        name_part = raw
        match = _YEAR_RE.search(raw)
        if match:
            year = int(match.group(0))
            name_part = (raw[: match.start()] + raw[match.end() :]).strip()
    name = normalize_name(re.sub(r"[\d,;]+", " ", name_part))
    if not name:
        return ParsedStudent(line_no, raw, "", year, "пустое имя")
    if len(name.split()) < 2:
        return ParsedStudent(line_no, raw, name, year, "нужны минимум фамилия и имя")
    if year is None:
        return ParsedStudent(line_no, raw, name, None, "не указан год поступления")
    return ParsedStudent(line_no, raw, name, year)


def parse_student_lines(text: str) -> list[ParsedStudent]:
    result: list[ParsedStudent] = []
    for index, line in enumerate(text.splitlines(), start=1):
        parsed = parse_student_line(index, line)
        if parsed is not None:
            result.append(parsed)
        if len(result) >= MAX_IMPORT_LINES:
            break
    return result


def parse_enrollment_year(value: str) -> int:
    raw = value.strip()
    if not re.fullmatch(r"(19|20)\d{2}", raw):
        raise ValueError("year")
    return int(raw)
