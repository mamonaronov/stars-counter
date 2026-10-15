from __future__ import annotations

from services.roles import (
    LEVEL_ADMIN,
    LEVEL_CHIEF_ADMIN,
    LEVEL_CHIEF_MARKER,
    LEVEL_MARKER,
    LEVEL_NONE,
    LEVEL_SUPERADMIN,
    ROLE_ADMIN,
    ROLE_CHIEF_ADMIN,
    ROLE_CHIEF_MARKER,
    ROLE_MARKER,
    Actor,
    role_after_transfer,
)
from tests.conftest import actor, student


def test_levels():
    assert actor(None).level == LEVEL_NONE
    assert actor(ROLE_MARKER).level == LEVEL_MARKER
    assert actor(ROLE_CHIEF_MARKER).level == LEVEL_CHIEF_MARKER
    assert actor(ROLE_ADMIN).level == LEVEL_ADMIN
    assert actor(ROLE_CHIEF_ADMIN).level == LEVEL_CHIEF_ADMIN
    assert actor(None, superadmin=True).level == LEVEL_SUPERADMIN
    guest = Actor(telegram_id=7, user=None, student=None, is_superadmin=False)
    assert guest.level == LEVEL_NONE and guest.role_name == "guest"


def test_removed_student_loses_rights():
    removed = Actor(telegram_id=7, user=None, student=student(role=ROLE_ADMIN, status="removed"), is_superadmin=False)
    assert removed.level == LEVEL_NONE


def test_assignable_roles_strictly_below():
    assert actor(ROLE_MARKER).assignable_roles() == []
    assert actor(ROLE_CHIEF_MARKER).assignable_roles() == [ROLE_MARKER]
    assert actor(ROLE_ADMIN).assignable_roles() == [ROLE_MARKER, ROLE_CHIEF_MARKER]
    assert actor(ROLE_CHIEF_ADMIN).assignable_roles() == [ROLE_MARKER, ROLE_CHIEF_MARKER, ROLE_ADMIN]
    assert actor(None, superadmin=True).assignable_roles() == [ROLE_MARKER, ROLE_CHIEF_MARKER, ROLE_ADMIN, ROLE_CHIEF_ADMIN]


def test_revoke_only_lower_levels_and_not_self():
    chief = actor(ROLE_CHIEF_MARKER, sid=10)
    assert chief.can_revoke(student(11, role=ROLE_MARKER))
    assert not chief.can_revoke(student(12, role=ROLE_CHIEF_MARKER))
    assert not chief.can_revoke(student(13, role=ROLE_ADMIN))
    assert not chief.can_revoke(student(10, role=ROLE_CHIEF_MARKER))
    assert not chief.can_revoke(student(14))


def test_transfer_rules():
    chief = actor(ROLE_CHIEF_MARKER, sid=10)
    assert chief.transferable_role == ROLE_CHIEF_MARKER
    assert chief.can_transfer_to(student(11, role=ROLE_MARKER, telegram_id=5))
    assert not chief.can_transfer_to(student(11, role=ROLE_MARKER))  # not verified
    assert not chief.can_transfer_to(student(12, role=ROLE_ADMIN, telegram_id=6))
    assert not chief.can_transfer_to(student(10, role=ROLE_CHIEF_MARKER, telegram_id=500))
    assert actor(ROLE_ADMIN).transferable_role is None
    assert actor(None, superadmin=True).transferable_role is None
    assert role_after_transfer(ROLE_CHIEF_MARKER) == ROLE_MARKER
    assert role_after_transfer(ROLE_CHIEF_ADMIN) == ROLE_ADMIN


def test_outranks_protects_higher_roles_and_superadmin():
    chief = actor(ROLE_CHIEF_MARKER, sid=10)
    assert chief.outranks(student(11, role=ROLE_MARKER, telegram_id=5), 1)
    assert chief.outranks(student(14), 1)
    assert chief.outranks(student(10, role=ROLE_CHIEF_MARKER, telegram_id=500), 1)  # own record
    assert not chief.outranks(student(12, role=ROLE_ADMIN, telegram_id=6), 1)
    assert not chief.outranks(student(13, role=ROLE_CHIEF_MARKER, telegram_id=7), 1)
    # superadmin's student row has no stored role, but must stay protected
    assert not chief.outranks(student(15, telegram_id=1), 1)
    assert actor(None, superadmin=True).outranks(student(12, role=ROLE_ADMIN, telegram_id=6), 1)


def test_capabilities():
    assert not actor(None).can_mark
    assert actor(ROLE_MARKER).can_mark and not actor(ROLE_MARKER).can_manage_students
    assert actor(ROLE_CHIEF_MARKER).can_manage_students
    assert actor(None, superadmin=True).can_manage_roles
