from __future__ import annotations

from services.periods import (
    OPEN_KEY,
    Period,
    build_periods,
    find_period,
    period_button_label,
    period_of_event,
    period_range_text,
    previous_closed_period,
)
from tests.conftest import TODAY, event


def test_no_events_gives_single_open_period():
    periods = build_periods([], TODAY)
    assert len(periods) == 1
    assert periods[0].key == OPEN_KEY
    assert not periods[0].closed
    assert periods[0].title == "Текущий: Октябрь 2026"
    assert period_range_text(periods[0]) == "нет занятий"


def test_closing_event_splits_periods():
    events = [
        event(1, "2026-09-01"),
        event(2, "2026-09-15"),
        event(3, "2026-09-29", closes=True),
        event(4, "2026-10-02"),
        event(5, "2026-10-06"),
    ]
    periods = build_periods(events, TODAY)
    assert [p.key for p in periods] == ["3", OPEN_KEY]
    september, current = periods
    assert september.closed and september.title == "Сентябрь 2026"
    assert [e.id for e in september.events] == [1, 2, 3]
    assert [e.id for e in current.events] == [4, 5]
    assert current.title == "Текущий: Октябрь 2026"
    assert period_of_event(periods, 4) is current
    assert find_period(periods, "3") is september
    assert period_range_text(september) == "01.09.2026 — 29.09.2026"
    assert previous_closed_period(periods) is september
    assert previous_closed_period(build_periods([], TODAY)) is None


def test_period_button_fits_telegram_limit():
    title = "Очень длинное название месяца " * 4
    period = Period(key="1", title=title, events=[event(1, "2026-09-01", closes=True)], closer=None)
    label = period_button_label(period)
    assert len(label) <= 64
    assert label.endswith("· 1 зан.")
    assert "Очень длинное" in label


def test_custom_month_title_wins():
    closer = event(2, "2026-10-01", closes=True)
    closer.month_title = "Сентябрь (сессия)"
    periods = build_periods([event(1, "2026-09-20"), closer], TODAY)
    assert periods[0].title == "Сентябрь (сессия)"


def test_order_uses_time_then_id_within_a_day():
    events = [
        event(3, "2026-09-01", time="12:00"),
        event(1, "2026-09-01", time="09:00", closes=True),
        event(2, "2026-09-01"),
    ]
    periods = build_periods(events, TODAY)
    # event without time sorts first, then 09:00 closes the month, 12:00 goes to the next period
    assert [e.id for e in periods[0].events] == [2, 1]
    assert [e.id for e in periods[1].events] == [3]


def test_mandatory_and_free_counts():
    periods = build_periods([event(1, "2026-09-01"), event(2, "2026-09-02", kind="free")], TODAY)
    assert periods[0].mandatory_count == 1
    assert periods[0].free_count == 1
