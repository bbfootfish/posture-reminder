"""Hourly cap of 0 means unlimited; cooldown still applies."""

from __future__ import annotations

from datetime import timedelta

from tests.helpers import HEAD_DOWN as HEAD_DOWN_SAMPLE
from tests.helpers import START, engine, feed


def test_zero_cap_allows_more_than_six_in_an_hour():
    machine = engine(smoothing_seconds=0.0, cooldown_seconds=1.0, hourly_cap=0,
           min_reminder_interval_seconds=0.0)
    reminders, last = feed(machine, START, 20.0, HEAD_DOWN_SAMPLE)
    assert len(reminders) == 1

    # With a 1 s cooldown and cap disabled, ten more triggers in the same hour
    # are all allowed (the old cap of 6 would have silenced after the 6th).
    more, last = feed(machine, last + timedelta(seconds=1.2), 30.0, HEAD_DOWN_SAMPLE)
    reminders.extend(more)
    assert len(reminders) >= 8
    assert machine.status != "hourly_cap"
    assert machine.snapshot(last)["hourly_unlimited"] is True


def test_positive_cap_still_enforced():
    machine = engine(smoothing_seconds=0.0, cooldown_seconds=1.0, hourly_cap=3,
           min_reminder_interval_seconds=0.0)
    reminders, last = feed(machine, START, 20.0, HEAD_DOWN_SAMPLE)
    more, last = feed(machine, last + timedelta(seconds=1.2), 30.0, HEAD_DOWN_SAMPLE)
    reminders.extend(more)
    assert len(reminders) == 3
    assert machine.status == "hourly_cap"
    assert machine.snapshot(last)["hourly_unlimited"] is False


def test_default_cap_is_unlimited():
    machine = engine(smoothing_seconds=0.0, cooldown_seconds=1.0,
           min_reminder_interval_seconds=0.0)
    assert machine.settings.hourly_cap == 0
    assert machine.snapshot(START)["hourly_unlimited"] is True
