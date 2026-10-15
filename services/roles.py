"""Role hierarchy and permission checks.

Levels:
    5 superadmin     (SUPERADMIN_TELEGRAM_ID in .env) — everything
    4 chief_admin    appoints admins, may hand over chief_admin
    3 admin          appoints chief markers
    2 chief_marker   appoints markers, manages students, may hand over chief_marker
    1 marker         marks attendance, events, templates, excuses, reports
    0 none           student or guest

Everyone at a higher level can do everything the lower levels can.
A role can be assigned or revoked only by someone strictly above it.
"""

from __future__ import annotations

from dataclasses import dataclass

from database.models import Student, User

LEVEL_NONE = 0
LEVEL_MARKER = 1
LEVEL_CHIEF_MARKER = 2
LEVEL_ADMIN = 3
LEVEL_CHIEF_ADMIN = 4
LEVEL_SUPERADMIN = 5

ROLE_MARKER = "marker"
ROLE_CHIEF_MARKER = "chief_marker"
ROLE_ADMIN = "admin"
ROLE_CHIEF_ADMIN = "chief_admin"
ROLE_SUPERADMIN = "superadmin"

ROLE_LEVELS: dict[str, int] = {
    ROLE_MARKER: LEVEL_MARKER,
    ROLE_CHIEF_MARKER: LEVEL_CHIEF_MARKER,
    ROLE_ADMIN: LEVEL_ADMIN,
    ROLE_CHIEF_ADMIN: LEVEL_CHIEF_ADMIN,
    ROLE_SUPERADMIN: LEVEL_SUPERADMIN,
}

ROLE_TITLES: dict[str, str] = {
    ROLE_MARKER: "отмечающий",
    ROLE_CHIEF_MARKER: "главный отмечающий",
    ROLE_ADMIN: "админ",
    ROLE_CHIEF_ADMIN: "главный админ",
    ROLE_SUPERADMIN: "суперадмин",
    "student": "студент",
    "guest": "гость",
}

# Roles that exist as a single "chief" seat and can be handed over.
TRANSFERABLE_ROLES = {ROLE_CHIEF_MARKER: ROLE_MARKER, ROLE_CHIEF_ADMIN: ROLE_ADMIN}

ASSIGNABLE_ROLES = (ROLE_MARKER, ROLE_CHIEF_MARKER, ROLE_ADMIN, ROLE_CHIEF_ADMIN)


def role_level(role: str | None) -> int:
    if not role:
        return LEVEL_NONE
    return ROLE_LEVELS.get(role, LEVEL_NONE)


def role_title(role: str | None) -> str:
    if not role:
        return ROLE_TITLES["student"]
    return ROLE_TITLES.get(role, role)


@dataclass(slots=True)
class Actor:
    telegram_id: int
    user: User | None
    student: Student | None
    is_superadmin: bool

    @property
    def level(self) -> int:
        if self.is_superadmin:
            return LEVEL_SUPERADMIN
        if self.student is None or not self.student.is_active:
            return LEVEL_NONE
        return role_level(self.student.role)

    @property
    def role_name(self) -> str:
        """Role string stored in the audit log."""
        if self.is_superadmin:
            return ROLE_SUPERADMIN
        if self.student is None or not self.student.is_active:
            return "guest"
        return self.student.role or "student"

    @property
    def role_title(self) -> str:
        return ROLE_TITLES.get(self.role_name, self.role_name)

    @property
    def display_name(self) -> str:
        if self.student is not None:
            return self.student.full_name
        if self.user is not None:
            return self.user.display
        return str(self.telegram_id)

    @property
    def is_verified_student(self) -> bool:
        return self.student is not None and self.student.is_active

    @property
    def can_mark(self) -> bool:
        return self.level >= LEVEL_MARKER

    @property
    def can_manage_students(self) -> bool:
        return self.level >= LEVEL_CHIEF_MARKER

    @property
    def can_manage_roles(self) -> bool:
        return self.level >= LEVEL_CHIEF_MARKER

    def can_assign(self, role: str) -> bool:
        if role not in ASSIGNABLE_ROLES:
            return False
        return role_level(role) < self.level

    def assignable_roles(self) -> list[str]:
        return [role for role in ASSIGNABLE_ROLES if self.can_assign(role)]

    def can_revoke(self, target: Student) -> bool:
        if not target.role:
            return False
        if self.student is not None and target.id == self.student.id:
            return False
        return role_level(target.role) < self.level

    def can_change_role_of(self, target: Student) -> bool:
        """Target must be below the actor; cannot edit own role here."""
        if self.student is not None and target.id == self.student.id:
            return False
        return role_level(target.role) < self.level

    def outranks(self, target: Student, superadmin_id: int) -> bool:
        """Whether this actor may change the student's record or cut off their access.

        Same or higher roles are protected. The superadmin from .env is protected
        from everyone else, even if their student row has no role stored.
        Editing your own record (name, year) is allowed; deleting yourself is not.
        """
        if target.telegram_id == superadmin_id and not self.is_superadmin:
            return False
        if self.student is not None and target.id == self.student.id:
            return True
        return role_level(target.role) < self.level

    @property
    def transferable_role(self) -> str | None:
        if self.is_superadmin or self.student is None:
            return None
        role = self.student.role
        return role if role in TRANSFERABLE_ROLES else None

    def can_transfer_to(self, target: Student) -> bool:
        role = self.transferable_role
        if role is None or not target.is_verified or not target.is_active:
            return False
        if self.student is not None and target.id == self.student.id:
            return False
        return role_level(target.role) < role_level(role)


def role_after_transfer(role: str) -> str:
    """What the former chief keeps after handing the seat over."""
    return TRANSFERABLE_ROLES.get(role, role)
