"""Student name normalisation and bulk import parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_SHORT_YEAR_AT_END = re.compile(r"(?:^|\s)(\d{2})\s*$")
MAX_IMPORT_LINES = 200
DIRECTION_LIMIT = 40
GROUP_LIMIT = 80
TRACK_LIMIT = 40
# Programs from the attendance journal. Markers can type any other value.
DIRECTIONS = ("ПИН", "ЭК", "ЮР")
# Column G of the journal: prayer track. The header in the register is «Направление».
TRACKS = ("молитва", "тфилин клуб")


def clean_profile_text(value: str, *, limit: int) -> str:
    text = " ".join(value.split())
    if not text:
        raise ValueError("empty")
    if len(text) > limit:
        raise ValueError("long")
    return text


def match_known(value: str, known: list[str]) -> str:
    for item in known:
        if item.casefold() == value.casefold():
            return item
    return value


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
    direction: str | None = None
    judaica_group: str | None = None
    track: str | None = None


def _enrollment_year(raw: str) -> int | None:
    """Accept 2023 and 23. A two-digit year is the 2000s, same as the journal."""
    text = raw.strip()
    if re.fullmatch(r"(19|20)\d{2}", text):
        return int(text)
    if re.fullmatch(r"\d{2}", text):
        return 2000 + int(text)
    return None


def _known_track(value: str) -> str | None:
    for item in TRACKS:
        if item.casefold() == value.casefold():
            return item
    return None


def _profile_tail(tail: str) -> tuple[int | None, str | None, str | None, str | None, str | None]:
    """Year, direction, track, Judaica group and an error from the text after the name."""
    parts = [part.strip() for part in re.split(r"[,;]", tail)]
    while parts and not parts[-1]:
        parts.pop()
    if not any(parts):
        return None, None, None, None, None
    year = _enrollment_year(parts[0])
    if year is None:
        match = _YEAR_RE.search(tail)
        filled = [part for part in parts if part]
        if match and len(filled) == 1:
            return int(match.group(0)), None, None, None, None
        if tail.strip():
            return None, None, None, None, f"не понял год: «{tail.strip()}»"
        return None, None, None, None, None
    direction = parts[1] if len(parts) > 1 and parts[1] else None
    rest = [part for part in parts[2:] if part]
    track = None
    if rest:
        known = _known_track(rest[0])
        if known is not None:
            track = known
            rest = rest[1:]
    group = ", ".join(rest) or None
    if direction is not None:
        try:
            direction = clean_profile_text(direction, limit=DIRECTION_LIMIT)
        except ValueError:
            return year, None, track, group, "направление слишком длинное"
    if group is not None:
        try:
            group = clean_profile_text(group, limit=GROUP_LIMIT)
        except ValueError:
            return year, direction, track, None, "группа иудаики слишком длинная"
    return year, direction, track, group, None


def parse_student_line(line_no: int, line: str) -> ParsedStudent | None:
    raw = line.strip().strip("•-–—").strip()
    if not raw:
        return None
    year: int | None = None
    direction: str | None = None
    group: str | None = None
    track: str | None = None
    separator = re.search(r"[,;]", raw)
    if separator:
        name_part, tail = raw[: separator.start()], raw[separator.end() :]
        year, direction, track, group, error = _profile_tail(tail)
        if error:
            return ParsedStudent(line_no, raw, normalize_name(name_part), year, error, direction, group, track)
    else:
        name_part = raw
        match = _YEAR_RE.search(raw)
        if match:
            year = int(match.group(0))
            name_part = (raw[: match.start()] + raw[match.end() :]).strip()
        else:
            short = _SHORT_YEAR_AT_END.search(raw)
            if short:
                year = 2000 + int(short.group(1))
                name_part = raw[: short.start(1)].strip()
    name = normalize_name(re.sub(r"[\d,;]+", " ", name_part))
    if not name:
        return ParsedStudent(line_no, raw, "", year, "пустое имя")
    if len(name.split()) < 2:
        return ParsedStudent(line_no, raw, name, year, "нужны минимум фамилия и имя")
    if year is None:
        return ParsedStudent(line_no, raw, name, None, "не указан год поступления")
    return ParsedStudent(line_no, raw, name, year, None, direction, group, track)


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
