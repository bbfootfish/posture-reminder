"""Time-of-day modes: night block, school block, late-night soft block."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from posture.schedule import (
    BlockWindow,
    DateRange,
    Mode,
    SchoolCalendar,
    ServiceSchedule,
)


def _schedule(*, breaks=()) -> ServiceSchedule:
    return ServiceSchedule(
        night=BlockWindow.parse("00:00-08:00"),
        school=BlockWindow.parse("08:00-18:00"),
        late_night=BlockWindow.parse("22:00-24:00"),
        calendar=SchoolCalendar(breaks=tuple(breaks)),
    )


# 2026-11-13 是周五（上学日）；2026-11-14 是周六；2026-10-01 国庆。
FRIDAY = date(2026, 11, 13)
SATURDAY = date(2026, 11, 14)
NATIONAL_DAY = date(2026, 10, 1)
# 2026-10-10 是调休补班的周六（已实测 is_workday=True）。
MAKEUP_SATURDAY = date(2026, 10, 10)


def at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second))


def test_school_day_boundaries_are_start_inclusive_end_exclusive():
    schedule = _schedule()
    assert schedule.mode(at(FRIDAY, 7, 59, 59)) is Mode.BLOCK   # 夜间
    assert schedule.mode(at(FRIDAY, 8, 0, 0)) is Mode.BLOCK     # 上学
    assert schedule.mode(at(FRIDAY, 17, 59, 59)) is Mode.BLOCK  # 上学
    assert schedule.mode(at(FRIDAY, 18, 0, 0)) is Mode.ALLOW


def test_night_block_applies_to_every_day():
    schedule = _schedule()
    for day in (FRIDAY, SATURDAY, NATIONAL_DAY):
        assert schedule.mode(at(day, 0, 0, 0)) is Mode.BLOCK
        assert schedule.mode(at(day, 7, 59, 59)) is Mode.BLOCK


def test_late_night_is_monitor_only_on_every_day():
    schedule = _schedule()
    for day in (FRIDAY, SATURDAY, NATIONAL_DAY):
        assert schedule.mode(at(day, 21, 59, 59)) is Mode.ALLOW
        assert schedule.mode(at(day, 22, 0, 0)) is Mode.MONITOR_ONLY
        assert schedule.mode(at(day, 23, 59, 59)) is Mode.MONITOR_ONLY


def test_weekend_daytime_is_allowed():
    schedule = _schedule()
    assert schedule.mode(at(SATURDAY, 10, 0)) is Mode.ALLOW
    assert schedule.explain(at(SATURDAY, 10, 0))["reason"] == "weekend"


def test_legal_holiday_daytime_is_allowed():
    schedule = _schedule()
    assert schedule.mode(at(NATIONAL_DAY, 10, 0)) is Mode.ALLOW
    info = schedule.explain(at(NATIONAL_DAY, 10, 0))
    assert info["reason"] == "holiday"
    assert info["holiday_name"]


def test_makeup_saturday_is_blocked_like_a_school_day():
    schedule = _schedule()
    assert schedule.mode(at(MAKEUP_SATURDAY, 10, 0)) is Mode.BLOCK
    assert schedule.explain(at(MAKEUP_SATURDAY, 10, 0))["reason"] == "school_hours"


def test_school_break_weekday_daytime_is_allowed():
    breaks = (DateRange.parse("2027-01-18", "2027-02-20", "寒假"),)
    schedule = _schedule(breaks=breaks)
    inside = date(2027, 1, 20)  # 周三
    assert schedule.mode(at(inside, 10, 0)) is Mode.ALLOW
    assert schedule.explain(at(inside, 10, 0))["reason"] == "school_break"
    # 端点含首尾
    assert schedule.mode(at(date(2027, 1, 18), 10, 0)) is Mode.ALLOW
    assert schedule.mode(at(date(2027, 2, 20), 10, 0)) is Mode.ALLOW
    # 区间外仍是上学日
    assert schedule.mode(at(date(2027, 2, 22), 10, 0)) is Mode.BLOCK


def test_calendar_out_of_range_fails_closed_and_warns_once(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="posture.schedule")
    schedule = _schedule()
    future = date(2028, 3, 1)  # 周三，超出 2004-2027
    caplog.clear()
    assert schedule.calendar_ok(future) is False
    assert schedule.is_school_day(future) is True
    assert schedule.mode(at(future, 10, 0)) is Mode.BLOCK

    warnings = [r for r in caplog.records if "calendar" in r.getMessage().lower()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]

    # 同一天再次查询不重复告警
    schedule.mode(at(future, 11, 0))
    warnings_again = [r for r in caplog.records if "calendar" in r.getMessage().lower()]
    assert len(warnings_again) == 1


def test_next_allowed_at_skips_blocked_windows():
    schedule = _schedule()
    # 周五上午（上学屏蔽）-> 当天 18:00
    assert schedule.next_allowed_at(at(FRIDAY, 10, 0)) == at(FRIDAY, 18, 0)
    # 周六凌晨（夜间屏蔽）-> 当天 08:00（周六白天允许）
    assert schedule.next_allowed_at(at(SATURDAY, 3, 0)) == at(SATURDAY, 8, 0)
    # 周五 22:30（软屏蔽）-> 次日 08:00（周六白天允许）
    assert schedule.next_allowed_at(at(FRIDAY, 22, 30)) == at(SATURDAY, 8, 0)


def test_next_allowed_at_from_sunday_night_lands_on_monday_evening():
    """周日夜到周一白天之间全是屏蔽，下次允许是周一 18:00。"""
    schedule = _schedule()
    sunday_night = at(date(2026, 11, 15), 23, 0)
    assert schedule.mode(sunday_night) is Mode.MONITOR_ONLY
    assert schedule.next_allowed_at(sunday_night) == at(date(2026, 11, 16), 18, 0)


def test_next_allowed_at_at_boundary_is_itself():
    """18:00 整已经允许；函数应原样返回，而不是跳到下一个窗口。"""
    schedule = _schedule()
    assert schedule.mode(at(FRIDAY, 18, 0)) is Mode.ALLOW
    assert schedule.next_allowed_at(at(FRIDAY, 18, 0)) == at(FRIDAY, 18, 0)


def test_explain_exposes_the_full_contract():
    schedule = _schedule()
    info = schedule.explain(at(FRIDAY, 10, 0))
    assert set(info) >= {
        "mode",
        "reason",
        "next_change_at",
        "next_allow_at",
        "school_day",
        "holiday_name",
        "calendar_ok",
    }
    assert info["mode"] == "block"
    assert info["school_day"] is True


def test_block_window_rejects_bad_input():
    with pytest.raises(ValueError):
        BlockWindow.parse("08:00")
    with pytest.raises(ValueError):
        BlockWindow.parse("25:00-26:00")


def test_midnight_window_includes_the_last_microsecond():
    """22:00-24:00 必须覆盖 23:59:59.999999，否则每天漏一微秒。"""
    window = BlockWindow.parse("22:00-24:00")
    assert window.contains(at(FRIDAY, 22, 0)) is True
    assert window.contains(datetime(2026, 11, 13, 23, 59, 59, 999999)) is True
    assert str(window) == "22:00-24:00"


def test_date_range_rejects_reversed_range():
    with pytest.raises(ValueError):
        DateRange.parse("2027-02-20", "2027-01-18", "寒假")


def test_service_schedule_summary_is_human_readable():
    schedule = _schedule()
    text = schedule.summary()
    assert "00:00-08:00" in text
    assert "08:00-18:00" in text
    assert "22:00-24:00" in text
