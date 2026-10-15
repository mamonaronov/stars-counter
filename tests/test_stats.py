from __future__ import annotations

from services.stats import (
    STATUS_ABSENT,
    STATUS_EXCUSED,
    STATUS_FREE_SKIPPED,
    STATUS_NOT_COUNTED,
    STATUS_PRESENT,
    compute_group_stats,
    compute_student_stats,
)
from tests.conftest import event, excuse, student


def test_basic_percent():
    s = student()
    events = [event(1, "2026-09-01"), event(2, "2026-09-03"), event(3, "2026-09-05"), event(4, "2026-09-07")]
    stats = compute_student_stats(s, events, {1, 2, 3}, [])
    assert stats.mandatory_total == 4
    assert stats.attended_mandatory == 3
    assert stats.percent == 75.0
    assert [r.status for r in stats.rows] == [STATUS_PRESENT] * 3 + [STATUS_ABSENT]


def test_free_events_only_count_when_attended():
    s = student()
    events = [event(1, "2026-09-01"), event(2, "2026-09-02", kind="free"), event(3, "2026-09-03", kind="free")]
    skipped = compute_student_stats(s, events, {1}, [])
    assert skipped.percent == 100.0
    assert skipped.rows[1].status == STATUS_FREE_SKIPPED
    attended = compute_student_stats(s, events, {1, 2}, [])
    assert attended.percent == 200.0
    assert attended.attended_free == 1


def test_excuse_counts_missed_mandatory_as_attended():
    s = student()
    events = [event(1, "2026-09-01"), event(2, "2026-09-05"), event(3, "2026-09-10")]
    stats = compute_student_stats(s, events, {1}, [excuse(1, "2026-09-04", "2026-09-06")])
    assert stats.excused == 1
    assert stats.rows[1].status == STATUS_EXCUSED
    assert stats.rows[2].status == STATUS_ABSENT
    assert stats.percent == round(2 / 3 * 100, 2)


def test_cancelled_excuse_is_ignored():
    s = student()
    events = [event(1, "2026-09-05")]
    stats = compute_student_stats(s, events, set(), [excuse(1, "2026-09-01", "2026-09-30", cancelled=True)])
    assert stats.excused == 0
    assert stats.percent == 0.0


def test_excuse_does_not_apply_to_free_events():
    s = student()
    events = [event(1, "2026-09-05", kind="free")]
    stats = compute_student_stats(s, events, set(), [excuse(1, "2026-09-01", "2026-09-30")])
    assert stats.rows[0].status == STATUS_FREE_SKIPPED
    assert stats.percent is None


def test_events_before_joined_on_are_not_counted():
    s = student(joined="2026-09-10")
    events = [event(1, "2026-09-01"), event(2, "2026-09-10"), event(3, "2026-09-12")]
    stats = compute_student_stats(s, events, {2}, [])
    assert stats.rows[0].status == STATUS_NOT_COUNTED
    assert stats.mandatory_total == 2
    assert stats.percent == 50.0


def test_events_after_left_on_are_not_counted():
    s = student(left="2026-09-05", status="removed")
    events = [event(1, "2026-09-01"), event(2, "2026-09-10")]
    stats = compute_student_stats(s, events, {1}, [])
    assert stats.rows[1].status == STATUS_NOT_COUNTED
    assert stats.percent == 100.0


def test_group_stats_sorted_desc():
    a = student(1, "Антонов Антон")
    b = student(2, "Борисов Борис")
    c = student(3, "Власов Влас")
    events = [event(1, "2026-09-01"), event(2, "2026-09-02")]
    attendance = {1: {1, 2}, 2: {1}}
    result = compute_group_stats([c, a, b], events, attendance, [])
    assert [r.student.id for r in result] == [1, 2, 3]
    assert [r.percent for r in result] == [100.0, 50.0, 0.0]
