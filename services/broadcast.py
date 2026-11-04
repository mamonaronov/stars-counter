"""Who receives a marker's message.

Selected groups are combined by union: a person gets the message when they
belong to at least one chosen group. «Все» is everyone who can be reached and
replaces the other choices. The sender is left out.
"""

from __future__ import annotations

from dataclasses import dataclass

from database.models import Student, User

FIXED_ORDER = ("all", "g", "s", "m", "cm", "a", "ca", "sa")
FIXED_LABELS = {
    "all": "Все",
    "g": "Гости",
    "s": "Студенты",
    "m": "Отмечающие",
    "cm": "Главные отмечающие",
    "a": "Админы",
    "ca": "Главные админы",
    "sa": "Суперадмин",
}
PROFILE_KINDS = ("d", "j", "t", "y")
PROFILE_TITLES = {
    "d": "Направление",
    "j": "Группа иудаики",
    "t": "Молитва / тфилин",
    "y": "Год поступления",
}
_ROLE_CODES = {
    "m": "marker",
    "cm": "chief_marker",
    "a": "admin",
    "ca": "chief_admin",
}


@dataclass(frozen=True, slots=True)
class AudienceCatalog:
    directions: tuple[str, ...]
    groups: tuple[str, ...]
    tracks: tuple[str, ...]
    years: tuple[int, ...]

    def values(self, kind: str) -> tuple[str, ...] | tuple[int, ...]:
        if kind == "d":
            return self.directions
        if kind == "j":
            return self.groups
        if kind == "t":
            return self.tracks
        if kind == "y":
            return self.years
        return ()


@dataclass(frozen=True, slots=True)
class BroadcastPerson:
    telegram_id: int
    student: Student | None
    is_superadmin: bool


def audience_catalog(students: list[Student]) -> AudienceCatalog:
    """Groups that have someone the bot can actually message."""
    reachable = [student for student in students if student.is_active and student.telegram_id is not None]
    return AudienceCatalog(
        directions=_unique(student.direction for student in reachable),
        groups=_unique(student.judaica_group for student in reachable),
        tracks=_unique(student.track for student in reachable),
        years=tuple(sorted({student.enrollment_year for student in reachable if student.enrollment_year}, reverse=True)),
    )


def broadcast_people(users: list[User], students: list[Student], superadmin_id: int) -> list[BroadcastPerson]:
    """Accounts that opened the bot, plus verified cards even if the user row is missing."""
    linked: dict[int, Student] = {}
    for student in students:
        if student.telegram_id is None:
            continue
        current = linked.get(student.telegram_id)
        if current is None or (not current.is_active and student.is_active):
            linked[student.telegram_id] = student
    ids = set(linked) | {user.telegram_id for user in users}
    people = [
        BroadcastPerson(
            telegram_id=telegram_id,
            student=linked.get(telegram_id),
            is_superadmin=telegram_id == superadmin_id,
        )
        for telegram_id in ids
    ]
    people.sort(key=lambda person: person.telegram_id)
    return people


def toggle_group(selected: set[str], code: str) -> set[str]:
    """«Все» stands alone. Any other group drops it."""
    chosen = set(selected)
    if code == "all":
        return set() if "all" in chosen else {"all"}
    chosen.discard("all")
    if code in chosen:
        chosen.remove(code)
    else:
        chosen.add(code)
    return chosen


def broadcast_recipients(
    people: list[BroadcastPerson],
    selected: set[str],
    catalog: AudienceCatalog,
    *,
    exclude: int | None = None,
) -> list[int]:
    if not selected:
        return []
    found: list[int] = []
    seen: set[int] = set()
    for person in people:
        if person.telegram_id == exclude or person.telegram_id in seen:
            continue
        if any(_matches(person, code, catalog) for code in selected):
            seen.add(person.telegram_id)
            found.append(person.telegram_id)
    return found


def option_label(code: str, catalog: AudienceCatalog) -> str | None:
    if code in FIXED_LABELS:
        return FIXED_LABELS[code]
    parsed = _profile(code)
    if parsed is None:
        return None
    kind, index = parsed
    values = catalog.values(kind)
    if index >= len(values):
        return None
    return f"{PROFILE_TITLES[kind]}: {values[index]}"


def selected_labels(selected: set[str], catalog: AudienceCatalog) -> list[str]:
    labels: list[str] = []
    for code in FIXED_ORDER:
        if code in selected:
            labels.append(FIXED_LABELS[code])
    for kind in PROFILE_KINDS:
        indexes = [index for code in selected if (index := _profile_index(code, kind)) is not None]
        for index in sorted(indexes):
            label = option_label(f"{kind}{index}", catalog)
            if label:
                labels.append(label)
    return labels


def known_code(code: str, catalog: AudienceCatalog) -> bool:
    if code in FIXED_LABELS:
        return True
    parsed = _profile(code)
    if parsed is None:
        return False
    kind, index = parsed
    return index < len(catalog.values(kind))


def _unique(values) -> tuple[str, ...]:
    found: list[str] = []
    for value in values:
        if value and value not in found:
            found.append(value)
    found.sort(key=str.casefold)
    return tuple(found)


def _profile(code: str) -> tuple[str, int] | None:
    if not code or code[0] not in PROFILE_KINDS:
        return None
    if not code[1:].isdigit():
        return None
    return code[0], int(code[1:])


def _profile_index(code: str, kind: str) -> int | None:
    parsed = _profile(code)
    if parsed is None or parsed[0] != kind:
        return None
    return parsed[1]


def _matches(person: BroadcastPerson, code: str, catalog: AudienceCatalog) -> bool:
    if code == "all":
        return True
    if code == "g":
        # The superadmin from .env has no student card, but is not a guest.
        return person.student is None and not person.is_superadmin
    if code == "sa" and person.is_superadmin:
        return True
    student = person.student
    if student is None or not student.is_active or student.telegram_id is None:
        return False
    if code == "s":
        return not student.role
    role = _ROLE_CODES.get(code)
    if role is not None:
        return student.role == role
    if code == "sa":
        return student.role == "superadmin"
    parsed = _profile(code)
    if parsed is None:
        return False
    kind, index = parsed
    values = catalog.values(kind)
    if index >= len(values):
        return False
    value = values[index]
    if kind == "d":
        return student.direction == value
    if kind == "j":
        return student.judaica_group == value
    if kind == "t":
        return student.track == value
    return student.enrollment_year == value
